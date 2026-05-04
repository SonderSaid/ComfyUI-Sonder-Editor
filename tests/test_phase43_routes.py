"""Route-level coverage for Phase 4.3 clip role fields."""

import asyncio
import importlib
import json
import os
import sys
from types import SimpleNamespace

from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import server.routes as routes
from server.timeline_state import (
    Asset,
    AudioTrack,
    BatchConfig,
    ClipReference,
    GenerationJob,
    GuideFrame,
    LaneConfig,
    PromptSection,
    Scene,
    TimelineProject,
)


class DummyRequest:
    def __init__(self, *, match_info=None, query=None, body=None):
        self.match_info = match_info or {}
        self.query = query or {}
        self._body = body

    async def json(self):
        return self._body


def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


def _response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_clip_post_put_role_validation_and_defaults(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [
        Asset(asset_id="asset-1", asset_type="video", path="media/clip.mp4", frame_count=12),
        Asset(asset_id="image-1", asset_type="image", path="media/ref.png", frame_count=1),
    ]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    add_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips",
    )
    update_clip = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}",
    )

    invalid = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "role": "bad"},
    )))
    assert invalid.status == 400

    invalid_driver_asset = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "image-1", "role": "motion_driver"},
    )))
    assert invalid_driver_asset.status == 400

    monkeypatch.setattr(
        route_module,
        "_extract_audio_from_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("motion drivers must not dual-drop audio")),
    )
    motion_driver = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={
            "asset_id": "asset-1",
            "role": "motion_driver",
            "timeline_start_frame": 1,
            "dual_drop": True,
        },
    )))
    motion_driver_json = _response_json(motion_driver)
    assert motion_driver.status == 201
    assert motion_driver_json["role"] == "motion_driver"
    assert "audio_track" not in motion_driver_json
    assert scene.audio_tracks == []

    created = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "timeline_start_frame": 3},
    )))
    created_json = _response_json(created)
    assert created_json["role"] == "render"
    assert created_json["strength"] == 1.0

    clip_id = created_json["clip_id"]
    invalid_update = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": clip_id},
        body={"role": "bad"},
    )))
    assert invalid_update.status == 400

    updated = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": clip_id},
        body={"role": "motion_driver", "strength": 0.42},
    )))
    updated_json = _response_json(updated)
    assert updated_json["role"] == "motion_driver"
    assert updated_json["strength"] == 0.42

    image_clip = ClipReference(clip_id="image-clip", source_path="media/ref.png")
    scene.clips.append(image_clip)
    invalid_image_update = asyncio.run(update_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "image-clip"},
        body={"role": "motion_driver"},
    )))
    assert invalid_image_update.status == 400


def test_dual_drop_skips_audio_when_video_asset_has_no_audio(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    scene = Scene(scene_id="scene-1", name="Scene")
    video_asset = Asset(
        asset_id="asset-1",
        name="silent.mp4",
        asset_type="video",
        path=os.path.join("media", "silent.mp4"),
        frame_count=12,
        has_audio=False,
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [video_asset]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)
    monkeypatch.setattr(
        route_module,
        "_extract_audio_from_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("no-audio assets must not extract")),
    )

    add_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips",
    )
    response = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "timeline_start_frame": 3, "dual_drop": True},
    )))
    payload = _response_json(response)

    assert response.status == 201
    assert "audio_track" not in payload
    assert len(scene.clips) == 1
    assert scene.audio_tracks == []
    assert [asset.asset_id for asset in project.assets] == ["asset-1"]


def test_dual_drop_rejects_partial_audio_extraction(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "media" / "with-audio.mp4").write_bytes(b"video")
    scene = Scene(scene_id="scene-1", name="Scene")
    video_asset = Asset(
        asset_id="asset-1",
        name="with-audio.mp4",
        asset_type="video",
        path=os.path.join("media", "with-audio.mp4"),
        frame_count=12,
        has_audio=True,
    )
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.assets = [video_asset]

    def fake_extract(_video_path, output_path):
        with open(output_path, "wb") as handle:
            handle.write(b"tiny")
        return True

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)
    monkeypatch.setattr(route_module, "_extract_audio_from_video", fake_extract)
    monkeypatch.setattr(
        route_module,
        "_get_audio_duration",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("tiny WAV should not be probed")),
    )

    add_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips",
    )
    response = asyncio.run(add_clip(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={"asset_id": "asset-1", "timeline_start_frame": 3, "dual_drop": True},
    )))
    payload = _response_json(response)
    audio_path = project_dir / "media" / "asset-1_audio.wav"

    assert response.status == 201
    assert "audio_track" not in payload
    assert len(scene.clips) == 1
    assert scene.audio_tracks == []
    assert [asset.asset_id for asset in project.assets] == ["asset-1"]
    assert not audio_path.exists()


def test_scene_put_accepts_motion_driver_lane_config(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    update_scene = _route_handler(
        route_module,
        "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}",
    )
    response = asyncio.run(update_scene(DummyRequest(
        match_info={"scene_id": "scene-1"},
        body={
            "motion_driver_lane_count": 2,
            "motion_driver_lane_configs": [
                {"name": "Driver", "color": "#2a9b9e", "locked": True, "hidden": True},
            ],
        },
    )))
    payload = _response_json(response)

    assert payload["motion_driver_lane_count"] == 2
    assert len(payload["motion_driver_lane_configs"]) == 2
    assert payload["motion_driver_lane_configs"][0] == {
        "name": "Driver",
        "color": "#2a9b9e",
        "locked": True,
        "hidden": True,
    }
    assert payload["motion_driver_lane_configs"][1] == {
        "name": "",
        "color": "",
        "locked": False,
        "hidden": False,
    }


def test_duplicate_scene_route_deep_copies_scene_and_regenerates_child_ids(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    source = Scene(
        scene_id="scene-1",
        name="Scene",
        order=3,
        duration_frames=48,
        prompt="scene prompt",
        generation_params={"seed": 123},
        batch_config=BatchConfig(max_frames=33, context_overlap=4, frame_alignment=8),
        asset_ids=["asset-1", "asset-2"],
        is_bridge=True,
        video_lane_count=2,
        motion_driver_lane_count=1,
        audio_lane_count=2,
        width=1280,
        height=720,
        fps=30.0,
    )
    source.prompt_sections = [PromptSection(start_frame=0, end_frame=24, prompt="section")]
    source.guide_frames = [GuideFrame(frame_index=8, asset_id="guide-1", source="asset", strength=0.75)]
    source.clips = [
        ClipReference(
            clip_id="clip-1",
            source_path="media/a.mp4",
            timeline_start_frame=4,
            timeline_end_frame=20,
            source_in_frame=2,
            source_out_frame=18,
            total_source_frames=32,
            source_origin_frame=2,
            opacity=0.8,
            track_index=1,
            role="render",
            strength=1.0,
            prompt="clip prompt",
            is_generated=True,
            generation_params={"cfg": 7},
            takes=["take-a"],
            active_take=0,
            take_metadata={"scene_id": "scene-1"},
        )
    ]
    source.audio_tracks = [
        AudioTrack(
            track_id="track-1",
            source_path="media/a.wav",
            timeline_start_frame=6,
            timeline_end_frame=30,
            source_in_frame=1,
            total_source_frames=48,
            source_origin_frame=1,
            volume=0.5,
            muted=True,
            lane_index=1,
        )
    ]
    source.video_lane_configs = [LaneConfig(name="V0"), LaneConfig(name="V1", hidden=True)]
    source.motion_driver_lane_configs = [LaneConfig(name="Driver", color="#ffaa00")]
    source.audio_lane_configs = [LaneConfig(name="A0"), LaneConfig(name="A1", locked=True)]
    source.saved_selections = [
        {"name": "Sel", "start": 4, "end": 20, "pre_context_frames": 2, "post_context_frames": 3}
    ]
    project = TimelineProject(
        project_dir=str(project_dir),
        name="Project",
        scenes=[Scene(scene_id="scene-0", name="Other", order=1), source],
    )

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    duplicate_scene = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/duplicate",
    )
    response = asyncio.run(duplicate_scene(DummyRequest(match_info={"scene_id": "scene-1"})))
    payload = _response_json(response)

    assert response.status == 201
    assert payload["scene_id"] != source.scene_id
    assert payload["name"] == "Scene (copy)"
    assert payload["order"] == 4
    assert len(project.scenes) == 3

    source_clip_ids = {clip.clip_id for clip in source.clips}
    duplicate_clip_ids = {clip["clip_id"] for clip in payload["clips"]}
    assert len(payload["clips"]) == len(source.clips)
    assert source_clip_ids.isdisjoint(duplicate_clip_ids)

    source_track_ids = {track.track_id for track in source.audio_tracks}
    duplicate_track_ids = {track["track_id"] for track in payload["audio_tracks"]}
    assert len(payload["audio_tracks"]) == len(source.audio_tracks)
    assert source_track_ids.isdisjoint(duplicate_track_ids)

    for key in [
        "duration_frames",
        "prompt",
        "prompt_sections",
        "generation_params",
        "batch_config",
        "guide_frames",
        "asset_ids",
        "is_bridge",
        "video_lane_count",
        "motion_driver_lane_count",
        "audio_lane_count",
        "video_lane_configs",
        "motion_driver_lane_configs",
        "audio_lane_configs",
        "width",
        "height",
        "fps",
        "saved_selections",
    ]:
        assert payload[key] == source.to_dict()[key]
    assert payload["clips"][0]["source_path"] == source.clips[0].source_path
    assert payload["clips"][0]["role"] == source.clips[0].role
    assert payload["audio_tracks"][0]["source_path"] == source.audio_tracks[0].source_path


def test_clip_split_preserves_motion_driver_role_and_strength(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    clip = ClipReference(
        clip_id="clip-1",
        source_path="media/driver.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        source_in_frame=0,
        source_out_frame=10,
        total_source_frames=10,
        track_index=0,
        role="motion_driver",
        strength=0.42,
    )
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.clips = [clip]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    split_clip = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}/split",
    )
    response = asyncio.run(split_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "clip-1"},
        body={"frame": 4},
    )))
    payload = _response_json(response)

    assert payload["left"]["role"] == "motion_driver"
    assert payload["right"]["role"] == "motion_driver"
    assert payload["left"]["strength"] == 0.42
    assert payload["right"]["strength"] == 0.42


def test_clear_queue_route_removes_only_completed_jobs(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = TimelineProject(project_dir=str(project_dir), name="Project")
    project.generation_queue = [
        GenerationJob(job_id="pending-1", status="pending"),
        GenerationJob(job_id="running-1", status="running"),
        GenerationJob(job_id="completed-1", status="completed"),
        GenerationJob(job_id="failed-1", status="failed"),
        GenerationJob(job_id="skipped-1", status="skipped"),
    ]

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    clear_queue = _route_handler(
        route_module,
        "DELETE",
        "/sonder-editor/project/{project_id}/queue",
    )
    response = asyncio.run(clear_queue(DummyRequest(match_info={"project_id": "project"})))
    payload = _response_json(response)

    assert payload == {"status": "cleared", "removed": 1}
    assert [job.job_id for job in project.generation_queue] == [
        "pending-1",
        "running-1",
        "failed-1",
        "skipped-1",
    ]


def test_delete_last_clip_compacts_empty_video_lane(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    delete_me = ClipReference(
        clip_id="delete-me",
        source_path="media/a.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        track_index=1,
        role="render",
    )
    higher = ClipReference(
        clip_id="higher",
        source_path="media/b.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        track_index=2,
        role="render",
    )
    driver = ClipReference(
        clip_id="driver",
        source_path="media/driver.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        track_index=1,
        role="motion_driver",
    )
    scene = Scene(scene_id="scene-1", name="Scene", video_lane_count=3)
    scene.video_lane_configs = [
        LaneConfig(name="Lane 1"),
        LaneConfig(name="Lane 2"),
        LaneConfig(name="Lane 3"),
    ]
    scene.clips = [delete_me, higher, driver]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    delete_clip = _route_handler(
        route_module,
        "DELETE",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}",
    )
    response = asyncio.run(delete_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "delete-me"},
    )))

    assert response.status == 200
    assert scene.video_lane_count == 2
    assert higher.track_index == 1
    assert driver.track_index == 1
    assert [config.name for config in scene.video_lane_configs] == ["Lane 1", "Lane 3"]


def test_delete_clip_preserve_lane_keeps_empty_video_lane(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    clip = ClipReference(
        clip_id="clip-1",
        source_path="media/a.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        track_index=1,
        role="render",
    )
    scene = Scene(scene_id="scene-1", name="Scene", video_lane_count=2)
    scene.video_lane_configs = [LaneConfig(name="Lane 1"), LaneConfig(name="Lane 2")]
    scene.clips = [clip]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    delete_clip = _route_handler(
        route_module,
        "DELETE",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}",
    )
    response = asyncio.run(delete_clip(DummyRequest(
        match_info={"scene_id": "scene-1", "clip_id": "clip-1"},
        query={"preserve_lane": "1"},
    )))

    assert response.status == 200
    assert scene.video_lane_count == 2
    assert scene.clips == []
    assert [config.name for config in scene.video_lane_configs] == ["Lane 1", "Lane 2"]


def test_delete_last_audio_track_compacts_empty_audio_lane(tmp_path, monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    delete_me = AudioTrack(
        track_id="delete-me",
        source_path="media/a.wav",
        timeline_start_frame=0,
        timeline_end_frame=10,
        lane_index=0,
    )
    higher = AudioTrack(
        track_id="higher",
        source_path="media/b.wav",
        timeline_start_frame=0,
        timeline_end_frame=10,
        lane_index=2,
    )
    scene = Scene(scene_id="scene-1", name="Scene", audio_lane_count=3)
    scene.audio_lane_configs = [
        LaneConfig(name="Audio 1"),
        LaneConfig(name="Audio 2"),
        LaneConfig(name="Audio 3"),
    ]
    scene.audio_tracks = [delete_me, higher]
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])

    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda project: None)

    delete_audio = _route_handler(
        route_module,
        "DELETE",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/audio_tracks/{track_id}",
    )
    response = asyncio.run(delete_audio(DummyRequest(
        match_info={"scene_id": "scene-1", "track_id": "delete-me"},
    )))

    assert response.status == 200
    assert scene.audio_lane_count == 2
    assert higher.lane_index == 1
    assert [config.name for config in scene.audio_lane_configs] == ["Audio 2", "Audio 3"]
