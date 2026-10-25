"""Placement geometry, lane allocation and generated-commit ownership regressions."""
from copy import deepcopy
import importlib

import pytest

from server import project_commit
from server.lane_registry import allocate_take_lane
from server.take_placement import calculate_take_placement
from server.timeline_state import Asset, AudioTrack, ClipReference, LaneConfig, Scene, TimelineProject


@pytest.mark.parametrize("family", ["video", "audio"])
@pytest.mark.parametrize("tail,expected", [("empty", 3), ("locked", 4), ("hidden", 4)])
def test_allocator_preserves_interior_holes_and_tail_configs(family, tail, expected):
    scene = Scene(video_lane_count=4, audio_lane_count=4,
                  video_lane_configs=[LaneConfig(name=str(i)) for i in range(4)],
                  audio_lane_configs=[LaneConfig(name=str(i)) for i in range(4)])
    scene.clips = [ClipReference(track_index=i) for i in (0, 2)] + [ClipReference(role="motion_driver", track_index=90)]
    scene.audio_tracks = [AudioTrack(lane_index=i) for i in (0, 2)]
    configs = getattr(scene, family + "_lane_configs")
    if tail != "empty":
        setattr(configs[3], tail, True)
    before = deepcopy(configs)
    assert allocate_take_lane(scene, family, LaneConfig) == expected
    assert configs[:4] == before


@pytest.mark.parametrize("drivers", [False, True])
def test_allocator_empty_scene_and_repeated_takes(drivers):
    scene = Scene(video_lane_count=1, video_lane_configs=[LaneConfig(name="Keep")])
    if drivers:
        scene.clips.append(ClipReference(role="motion_driver", track_index=7))
    for expected in range(3):
        lane = allocate_take_lane(scene, "video", LaneConfig)
        assert lane == expected
        scene.clips.append(ClipReference(track_index=lane))
    assert scene.video_lane_count == 3 and scene.video_lane_configs[0].name == "Keep"


@pytest.mark.parametrize("mode,before,after,count,padding,expected", [
    ("trimmed", 0, 0, 33, 0, (20, 33, 20, 33, 33)),
    ("trimmed", 3, 4, 44, 3, (17, 37, 17, 37, 41)),
    ("trimmed", 99, 99, 44, 3, (0, 41, 0, 41, 41)),
    ("trimmed", 0, 0, 25, 0, (20, 25, 20, 25, 25)),
    ("trimmed", 0, 0, 12, 0, (20, 20, 12, 12, 12)),
    ("untrimmed", 3, 4, 44, 3, (0, 41, 0, 41, 41)),
])
def test_authored_geometry_ignores_model_snapping(mode, before, after, count, padding, expected):
    result = calculate_take_placement({
        "selection_start": 20, "selection_end": 33,
        "actual_pre_context_frames": 20, "actual_post_context_frames": 8,
        "requested_mask_pre_offset": before, "requested_mask_post_offset": after,
        "mask_pre_offset": 20, "mask_post_offset": 8,
        "mask_start_frame": 17, "mask_end_frame": 41,
        "take_placement_mode": mode, "frame_count_padding": padding,
    }, count)
    assert tuple(result[k] for k in ("timeline_start_frame", "timeline_end_frame", "source_in_frame", "source_out_frame", "total_source_frames")) == expected
    assert result["source_origin_frame"] == 0


def test_conflict_allocates_only_declared_takes_and_preserves_producer_snapshot():
    current_scene = Scene(scene_id="s", video_lane_count=4,
                          video_lane_configs=[LaneConfig(), LaneConfig(), LaneConfig(locked=True), LaneConfig()],
                          clips=[ClipReference(clip_id="live", track_index=1, source_in_frame=7),
                                 ClipReference(clip_id="driver", role="motion_driver", track_index=9)],
                          audio_lane_count=2, audio_lane_configs=[LaneConfig(), LaneConfig(hidden=True)],
                          audio_tracks=[AudioTrack(track_id="live-a", lane_index=0)])
    current = TimelineProject(scenes=[current_scene])
    produced = deepcopy(current)
    ps = produced.scenes[0]
    ps.clips[0].source_in_frame = 0  # concurrent manual trim belongs to current
    ps.clips += [ClipReference(clip_id="deleted", is_generated=True),
                 ClipReference(clip_id="explicit", is_generated=True, track_index=0),
                 ClipReference(clip_id="new-driver", role="motion_driver", track_index=0, is_generated=True),
                 ClipReference(clip_id="take1", track_index=1, is_generated=True),
                 ClipReference(clip_id="take2", track_index=2, is_generated=True)]
    ps.audio_tracks += [AudioTrack(track_id="a1", lane_index=1), AudioTrack(track_id="a2", lane_index=2)]
    ps.linked_item_groups = [{"group_id": "g", "items": [{"type":"clip", "id":"take1"}, {"type":"audio", "id":"a1"}]}]
    created = {"clips": {"explicit", "new-driver", "take1", "take2"}, "audio": {"a1", "a2"}, "assets": set(), "links": {"g"}}
    auto = {"clips": {"take1", "take2"}, "audio": {"a1", "a2"}}
    before = deepcopy(produced.to_dict())
    assert project_commit._merge_generated_outputs(current, produced, created, auto_placement_ids=auto)
    clips = {c.clip_id:c for c in current_scene.clips}
    assert "deleted" not in clips and clips["live"].source_in_frame == 7
    assert (clips["explicit"].track_index, clips["new-driver"].track_index) == (0, 0)
    assert (clips["take1"].track_index, clips["take2"].track_index) == (3, 4)
    assert [a.lane_index for a in current_scene.audio_tracks] == [0, 2, 3]
    assert current_scene.linked_item_groups == ps.linked_item_groups
    assert produced.to_dict() == before


def test_retry_reallocates_against_each_latest_scene_without_mutating_source(monkeypatch):
    produced = TimelineProject(project_dir="unused", scenes=[Scene(scene_id="s", clips=[ClipReference(clip_id="take", track_index=1, is_generated=True)])])
    states = [TimelineProject(modified_at=str(i), scenes=[Scene(scene_id="s", video_lane_count=i+2,
               video_lane_configs=[LaneConfig() for _ in range(i+2)],
               clips=[ClipReference(clip_id="live", track_index=i+1)])]) for i in range(2)]
    before = deepcopy(produced.to_dict())
    saves = []
    def save(project, **kwargs):
        saves.append(project)
        if len(saves) < 3:
            raise project_commit.ProjectVersionConflict(project_dir="unused", expected_modified_at="old", actual_modified_at="new")
    monkeypatch.setattr(project_commit, "save_project", save)
    monkeypatch.setattr(project_commit, "load_project", lambda _: states.pop(0))
    result = project_commit.save_generated_project(produced, "base", created_ids={"clips":{"take"}}, auto_placement_ids={"clips":{"take"}})
    assert [s.scenes[0].clips[-1].track_index for s in saves] == [1, 2, 3]
    assert result.scenes[0].video_lane_count == 4
    assert produced.to_dict() == before


@pytest.mark.parametrize("encoding_fps", [24.0, 30.0])
def test_real_editor_context_and_save_video_share_authored_av_geometry(tmp_path, monkeypatch, encoding_fps):
    from test_editor_node import TEST_PACKAGE, _FrameConstraintProject, _import_editor_node, _import_io_nodes, _patch_render_and_audio, _fake_encode_video_success
    editor = _import_editor_node(tmp_path, monkeypatch)
    io = _import_io_nodes(tmp_path, monkeypatch)
    torch = importlib.import_module("torch")
    project = _FrameConstraintProject(tmp_path, template_id="ltx-2.3", frame_constraint={"step":8, "offset":1, "min":1})
    monkeypatch.setattr(editor, "load_project", lambda _:project)
    _patch_render_and_audio(editor, monkeypatch)
    editor.SonderEditor().execute(project="Existing Project", project_name="Ignored", fps=24.0, width=32, height=32,
        scene_id="scene-1", selection_start=20, selection_end=33, pre_context_frames=49, post_context_frames=0,
        mask_pre_offset=0, mask_post_offset=0)
    ctx = project._execution_context
    assert ctx["mask_start_frame"] == 17  # model behavior is unchanged
    assert ctx["requested_mask_pre_offset"] == 0 and ctx["placement_fps"] == 24.0
    state = importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    scene = state.Scene(scene_id="scene-1", duration_frames=100,
                        clips=[state.ClipReference(role="motion_driver", track_index=7)])
    real = state.TimelineProject(project_dir=str(tmp_path / "project"), scenes=[scene], fps=24.0)
    (tmp_path / "project" / "media").mkdir(parents=True)
    real._execution_context = dict(ctx, base_modified_at="")
    monkeypatch.setattr(io, "encode_video", _fake_encode_video_success(io))
    monkeypatch.setattr(io, "save_project", lambda _:None)
    monkeypatch.setattr(io, "_build_take_proxy", lambda *a:None)
    thumbs = importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")
    monkeypatch.setattr(thumbs, "ensure_thumbnail", lambda *a, **k:None)
    audio = {"waveform": torch.zeros(1, 2, 66000), "sample_rate":48000}
    result = io.SonderSaveVideo().save_video(real, torch.zeros(33, 2, 2, 3), fps=encoding_fps, mode="Take", audio=audio)
    clip = scene.clips[-1]
    track = scene.audio_tracks[-1]
    assert (clip.track_index, scene.video_lane_count) == (0, 1)
    for item in (clip, track):
        assert (item.timeline_start_frame, item.timeline_end_frame, item.source_in_frame, item.total_source_frames, item.source_origin_frame) == (20, 33, 20, 33, 0)
    assert result["ui"]["sonder_video"][0]["scene_map"] == {"scene_id":"scene-1", "scene_start":20, "scene_end":33, "source_offset":20, "fps":encoding_fps}
    video, audio_asset = real.assets
    assert video.fps == encoding_fps
    assert video.generation_params["take_placement_policy"] == "authored_v1"
    assert audio_asset.generation_params["source_video_asset_id"] == video.asset_id
    assert audio_asset.generation_params["encoded_frame_count"] == 33


def test_timeline_export_ignores_driver_and_skips_locked_tail():
    from server.timeline_export import _place_video_take
    scene = Scene(video_lane_count=2, video_lane_configs=[LaneConfig(locked=True), LaneConfig()],
                  clips=[ClipReference(role="motion_driver", track_index=7)])
    asset = Asset(path="media/export.mp4", frame_count=13, generation_params={"editor_export":{"range":{"start":20,"end":33},"fps":24}})
    clip = _place_video_take(TimelineProject(), scene, asset, 20, 33)
    assert (clip.track_index, scene.video_lane_count) == (1, 2)
    assert (clip.source_in_frame, clip.source_out_frame, clip.timeline_start_frame, clip.timeline_end_frame) == (0, 13, 20, 33)


@pytest.mark.parametrize("family", ["video", "audio"])
@pytest.mark.parametrize("usable_surplus", [False, True])
def test_allocator_respects_retained_surplus_configs(family, usable_surplus):
    scene = Scene()
    configs = [LaneConfig(), LaneConfig(name="preserved", locked=True, hidden=True)]
    if usable_surplus:
        configs.append(LaneConfig(name="usable"))
    setattr(scene, family + "_lane_configs", configs)
    if family == "video":
        scene.clips = [ClipReference(track_index=0)]
    else:
        scene.audio_tracks = [AudioTrack(lane_index=0)]
    assert allocate_take_lane(scene, family, LaneConfig) == 2
    assert getattr(scene, family + "_lane_count") == 3
    assert configs[1] == LaneConfig(name="preserved", locked=True, hidden=True)
    assert not configs[2].locked and not configs[2].hidden
    assert configs[2].name == ("usable" if usable_surplus else "")


def test_conflict_avoids_surplus_locked_config():
    current = TimelineProject(scenes=[Scene(scene_id="s", clips=[ClipReference(track_index=0)],
        video_lane_configs=[LaneConfig(), LaneConfig(locked=True, hidden=True)])])
    produced = TimelineProject(scenes=[Scene(scene_id="s", clips=[ClipReference(clip_id="take", is_generated=True)])])
    project_commit._merge_generated_outputs(current, produced, {"clips":{"take"}}, auto_placement_ids={"clips":{"take"}})
    assert current.scenes[0].clips[-1].track_index == 2
    assert current.scenes[0].video_lane_configs[1].locked


def test_short_export_audio_uses_video_geometry_and_preserves_actual_sidecar_duration(tmp_path, monkeypatch):
    import numpy as np
    from server import timeline_export, media_helpers
    (tmp_path / "media").mkdir()
    (tmp_path / "media/video.mp4").write_bytes(b"video")
    prepared = tmp_path / "prepared.wav"
    media_helpers.write_audio_wav(prepared, np.zeros((2,26000),np.float32),48000)
    scene = Scene(scene_id="s")
    video = Asset(asset_id="video", path="media/video.mp4", asset_type="video", frame_count=10, fps=24, has_audio=True)
    project = TimelineProject(project_dir=str(tmp_path), fps=24, scenes=[scene], assets=[video])
    monkeypatch.setattr(timeline_export, "ensure_thumbnail", lambda *a, **k:True)
    clip = timeline_export._place_video_take(project, scene, video, 20, 33)
    track, _ = timeline_export._place_embedded_audio_take(project, scene, video, 20, 33, "", {}, prepared_audio_path=str(prepared))
    for item in (clip, track):
        assert (item.timeline_start_frame,item.timeline_end_frame,item.source_in_frame,item.total_source_frames) == (20,30,0,10)
    assert project.assets[-1].duration_sec == pytest.approx(13/24)
    assert project.assets[-1].generation_params["encoded_frame_count"] == 10


def test_export_completion_reports_committed_lane(tmp_path, monkeypatch):
    import numpy as np
    from server import timeline_export, project_manager
    (tmp_path / "media").mkdir()
    project = TimelineProject(project_dir=str(tmp_path), scenes=[Scene(scene_id="s",duration_frames=4)], resolution=(2,2))
    project_manager.save_project(project)
    monkeypatch.setattr(timeline_export,"iter_scene_frames",lambda *a,**k:iter(np.zeros((4,2,2,3),np.uint8)))
    def encode(frames_iter,*,output_path,**kwargs):
        from pathlib import Path
        list(frames_iter);Path(output_path).write_bytes(b"video");return {"has_audio":False}
    monkeypatch.setattr(timeline_export,"encode_video",encode)
    monkeypatch.setattr(timeline_export,"ensure_thumbnail",lambda *a,**k:True)
    monkeypatch.setattr(timeline_export,"_technical_video_metadata",lambda p,f:dict(f))
    commit=timeline_export.save_generated_project
    def conflict(produced,*args,**kwargs):
        current=project_manager.load_project(str(tmp_path))
        current.scenes[0].clips.append(ClipReference(clip_id="concurrent",track_index=0,timeline_end_frame=4))
        project_manager.save_project(current)
        return commit(produced,*args,**kwargs)
    monkeypatch.setattr(timeline_export,"save_generated_project",conflict)
    manager=timeline_export.TimelineExportManager(max_workers=1)
    job=manager.start(project,{"scene_id":"s","range":{"start":0,"end":4},"include_video":True,"include_audio":False,"place_as_take":True,"save_preset":"Compatible MP4"})
    job.future.result(timeout=10)
    assert job.status == "completed",job.error
    saved=project_manager.load_project(str(tmp_path))
    actual=next(c for c in saved.scenes[0].clips if c.clip_id==job.placed_clip["clip_id"])
    assert actual.track_index == 1
    assert job.placed_clip == actual.to_dict()


def test_save_video_without_selected_source_frames_warns_without_creating_lanes(tmp_path, monkeypatch):
    from test_editor_node import TEST_PACKAGE, _import_io_nodes, _fake_encode_video_success
    io=_import_io_nodes(tmp_path,monkeypatch)
    state=importlib.import_module(f"{TEST_PACKAGE}.server.timeline_state")
    torch=importlib.import_module("torch")
    (tmp_path/"media").mkdir()
    scene=state.Scene(scene_id="s")
    project=state.TimelineProject(project_dir=str(tmp_path),scenes=[scene])
    project._execution_context={"scene_id":"s","selection_start":20,"selection_end":33,"actual_pre_context_frames":20}
    monkeypatch.setattr(io,"encode_video",_fake_encode_video_success(io))
    monkeypatch.setattr(io,"save_project",lambda _:None)
    monkeypatch.setattr(io,"_build_take_proxy",lambda *a:None)
    thumbs=importlib.import_module(f"{TEST_PACKAGE}.server.thumbnail_service")
    monkeypatch.setattr(thumbs,"ensure_thumbnail",lambda *a,**k:None)
    before=(scene.video_lane_count,scene.audio_lane_count)
    result=io.SonderSaveVideo().save_video(project,torch.zeros(12,2,2,3),mode="Take",audio={"waveform":torch.zeros(1,2,24000),"sample_rate":48000})
    assert not scene.clips and not scene.audio_tracks
    assert (scene.video_lane_count,scene.audio_lane_count)==before
    assert len(project.assets)==1 and project.assets[0].asset_type=="video"
    assert not list((tmp_path/"media").glob('*_audio.wav'))
    assert result["ui"]["sonder_video"][0]["alerts"]


def test_asset_remaps_are_independent_across_retries():
    video=Asset(asset_id="produced-video",path="media/take.mp4",generation_params={"take_placement_policy":"authored_v1"})
    audio=Asset(asset_id="produced-audio",path="media/take.wav",asset_type="audio",generation_params={"source_video_asset_id":"produced-video","encoded_frame_count":33})
    produced=TimelineProject(assets=[video,audio])
    before=deepcopy(produced.to_dict())
    for placeholder in ("first-placeholder","second-placeholder"):
        current=TimelineProject(assets=[Asset(asset_id=placeholder,path="media/take.mp4")])
        remap={}
        project_commit._merge_generated_outputs(current,produced,{"assets":{"produced-video","produced-audio"}},asset_id_remap=remap)
        assert current.get_asset("produced-audio").generation_params["source_video_asset_id"]==placeholder
        assert produced.to_dict()==before
