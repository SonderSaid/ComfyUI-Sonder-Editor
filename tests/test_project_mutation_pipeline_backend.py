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
from server.timeline_state import Asset, AudioTrack, ClipReference, GenerationJob, GuideFrame, LaneConfig, PromptSection, Scene, TimelineProject


class DummyRequest(dict):
    def __init__(self, *, match_info=None, query=None, body=None, method="POST", path="/sonder-editor/project/proj", headers=None):
        super().__init__()
        self.match_info = match_info or {}
        self.query = query or {}
        self.headers = headers or {}
        self._body = body
        self.method = method
        self.path = path

    async def json(self):
        return self._body


def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(
        instance=SimpleNamespace(routes=web.RouteTableDef(), app=web.Application())
    )
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


def _response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_project_version_header_middleware_attaches_loaded_project_version(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir="", name="Project")
    project.project_id = "project-1"
    project.modified_at = "version-1"

    async def handler(request):
        route_module._remember_request_project(request, project)
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        match_info={"project_id": "project-1"},
        method="GET",
        path="/sonder-editor/project/project-1/scenes/scene-1",
    )
    response = asyncio.run(route_module._project_version_header_middleware(request, handler))

    assert response.headers["X-Sonder-Project-Id"] == "project-1"
    assert response.headers["X-Sonder-Project-Modified-At"] == "version-1"


def test_sonder_security_middleware_adds_security_headers(monkeypatch):
    route_module = _load_route_module(monkeypatch)

    async def handler(_request):
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="GET",
        path="/sonder-editor/project/project-1/assets",
        headers={"Host": "127.0.0.1:7822"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]


def test_sonder_security_middleware_blocks_cross_origin_mutation(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    called = []

    async def handler(_request):
        called.append(True)
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="POST",
        path="/sonder-editor/project/project-1/assets/sync",
        headers={"Host": "127.0.0.1:7822", "Origin": "https://example.invalid"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))
    payload = _response_json(response)

    assert response.status == 403
    assert payload["code"] == "cross_origin_blocked"
    assert called == []
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_sonder_security_middleware_allows_same_origin_mutation(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    called = []

    async def handler(_request):
        called.append(True)
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="POST",
        path="/sonder-editor/project/project-1/assets/sync",
        headers={"Host": "127.0.0.1:7822", "Origin": "http://127.0.0.1:7822"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))

    assert response.status == 200
    assert called == [True]


def test_sonder_security_middleware_allows_absent_origin_mutation(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    called = []

    async def handler(_request):
        called.append(True)
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="POST",
        path="/sonder-editor/project/project-1/assets/sync",
        headers={"Host": "127.0.0.1:7822"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))

    assert response.status == 200
    assert called == [True]


def test_scene_mutation_remove_lane_is_single_save_and_reindexes(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig(name="V0"), LaneConfig(name="V1"), LaneConfig(name="V2")]
    scene.clips = [
        ClipReference(clip_id="clip-0", timeline_start_frame=0, timeline_end_frame=10, track_index=0),
        ClipReference(clip_id="clip-1", timeline_start_frame=0, timeline_end_frame=10, track_index=1),
        ClipReference(clip_id="clip-2", timeline_start_frame=0, timeline_end_frame=10, track_index=2),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={
            "operations": [{
                "type": "remove_lane",
                "lane_type": "video",
                "lane_index": 1,
                "item_policy": "move_items",
                "target_lane": 0,
            }],
        },
    )))

    assert response.status == 200
    assert len(saves) == 1
    assert scene.video_lane_count == 2
    assert [cfg.name for cfg in scene.video_lane_configs] == ["V0", "V2"]
    assert {clip.clip_id: clip.track_index for clip in scene.clips} == {
        "clip-0": 0,
        "clip-1": 0,
        "clip-2": 1,
    }


def test_scene_mutation_guide_identity_mismatch_rejects_before_save(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.guide_frames = [GuideFrame(frame_index=5, asset_id="asset-a", strength=0.7)]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={
            "operations": [{
                "type": "move_guide",
                "from_frame_index": 5,
                "to_frame_index": 8,
                "expected": {"frame_index": 5, "asset_id": "different"},
            }],
        },
    )))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["code"] == "identity_mismatch"
    assert saves == []
    assert [(guide.frame_index, guide.asset_id) for guide in scene.guide_frames] == [(5, "asset-a")]


def test_scene_mutation_move_guide_replaces_destination_frame(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.guide_frames = [
        GuideFrame(frame_index=5, asset_id="asset-a", strength=0.7),
        GuideFrame(frame_index=8, asset_id="asset-b", strength=0.4),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={
            "operations": [{
                "type": "move_guide",
                "from_frame_index": 5,
                "to_frame_index": 8,
                "expected": {"frame_index": 5, "asset_id": "asset-a"},
                "asset_id": "asset-a",
                "source": "asset",
                "strength": 0.7,
            }],
        },
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert len(saves) == 1
    assert [(guide.frame_index, guide.asset_id) for guide in scene.guide_frames] == [(8, "asset-a")]
    assert [(guide["frame_index"], guide["asset_id"]) for guide in payload["scene"]["guide_frames"]] == [(8, "asset-a")]


def test_scene_mutation_linked_move_propagates_to_mixed_items(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=80)
    prompt = PromptSection(start_frame=0, end_frame=20, prompt="section")
    prompt.prompt_id = "prompt-1"
    scene.clips = [ClipReference(
        clip_id="clip-1",
        timeline_start_frame=0,
        timeline_end_frame=20,
        source_in_frame=4,
        source_out_frame=24,
    )]
    scene.audio_tracks = [AudioTrack(
        track_id="audio-1",
        timeline_start_frame=0,
        timeline_end_frame=20,
        source_in_frame=3,
    )]
    scene.guide_frames = [GuideFrame(guide_id="guide-1", frame_index=5, asset_id="asset-a")]
    scene.prompt_sections = [prompt]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _mutation_handler(route_module)
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [
            {
                "type": "create_link_group",
                "items": [
                    {"type": "clip", "id": "clip-1"},
                    {"type": "audio", "id": "audio-1"},
                    {"type": "guide", "id": "guide-1"},
                    {"type": "prompt", "id": "prompt-1"},
                ],
            },
            {
                "type": "update_clip",
                "clip_id": "clip-1",
                "fields": {"timeline_start_frame": 10, "timeline_end_frame": 30},
                "apply_linked": True,
            },
        ]},
    )))

    assert response.status == 200
    assert len(saves) == 1
    assert scene.clips[0].timeline_start_frame == 10
    assert scene.clips[0].timeline_end_frame == 30
    assert scene.audio_tracks[0].timeline_start_frame == 10
    assert scene.audio_tracks[0].timeline_end_frame == 30
    assert scene.guide_frames[0].frame_index == 15
    assert scene.prompt_sections[0].start_frame == 10
    assert scene.prompt_sections[0].end_frame == 30
    assert scene.clips[0].source_in_frame == 4
    assert scene.clips[0].source_out_frame == 24
    assert scene.audio_tracks[0].source_in_frame == 3


def test_scene_mutation_linked_move_rejects_locked_member(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.audio_lane_configs = [LaneConfig(locked=True)]
    scene.clips = [ClipReference(clip_id="clip-1", timeline_start_frame=0, timeline_end_frame=20)]
    scene.audio_tracks = [AudioTrack(track_id="audio-1", timeline_start_frame=0, timeline_end_frame=20)]
    scene.linked_item_groups = [{
        "group_id": "group-1",
        "items": [{"type": "clip", "id": "clip-1"}, {"type": "audio", "id": "audio-1"}],
    }]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _mutation_handler(route_module)
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_clip",
            "clip_id": "clip-1",
            "fields": {"timeline_start_frame": 10, "timeline_end_frame": 30},
            "apply_linked": True,
        }]},
    )))

    assert response.status == 409
    assert _response_json(response)["code"] == "track_locked"
    assert saves == []
    assert scene.clips[0].timeline_start_frame == 0
    assert scene.audio_tracks[0].timeline_start_frame == 0


def test_scene_mutation_replace_clip_source_clamps_and_clears_generated_provenance(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    clip = ClipReference(
        clip_id="clip-1",
        source_path="media/old.mp4",
        timeline_start_frame=10,
        timeline_end_frame=70,
        source_in_frame=20,
        source_out_frame=80,
        total_source_frames=120,
        source_origin_frame=20,
        opacity=0.5,
        track_index=2,
        is_generated=True,
        generation_params={"seed": 1},
        takes=[{"asset_id": "old"}],
        active_take=1,
        take_metadata={"scene_id": "scene-1"},
    )
    scene.clips = [clip]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    project.assets = [
        Asset(asset_id="old", asset_type="video", path="media/old.mp4", frame_count=120),
        Asset(asset_id="new", asset_type="video", path="media/new.mp4", frame_count=45),
    ]
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    response = asyncio.run(_mutation_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "replace_clip_source",
            "clip_id": "clip-1",
            "asset_id": "new",
        }]},
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert len(saves) == 1
    assert clip.source_path == "media/new.mp4"
    assert clip.timeline_start_frame == 10
    assert clip.timeline_end_frame == 35
    assert clip.source_in_frame == 20
    assert clip.source_out_frame == 45
    assert clip.total_source_frames == 45
    assert clip.source_origin_frame == 0
    assert clip.opacity == 0.5
    assert clip.track_index == 2
    assert clip.is_generated is False
    assert clip.generation_params == {}
    assert clip.takes == []
    assert clip.active_take == 0
    assert clip.take_metadata == {}
    assert payload["scene"]["clips"][0]["source_path"] == "media/new.mp4"


def test_scene_mutation_replace_audio_source_clamps_and_preserves_track_edits(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    track = AudioTrack(
        track_id="audio-1",
        source_path="media/old.wav",
        timeline_start_frame=5,
        timeline_end_frame=65,
        source_in_frame=30,
        total_source_frames=120,
        source_origin_frame=30,
        volume=0.25,
        muted=True,
        lane_index=3,
    )
    scene.audio_tracks = [track]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", fps=24, scenes=[scene])
    project.assets = [
        Asset(asset_id="old", asset_type="audio", path="media/old.wav", duration_sec=5.0),
        Asset(asset_id="new", asset_type="audio", path="media/new.wav", duration_sec=2.0),
    ]
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    response = asyncio.run(_mutation_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "replace_audio_source",
            "track_id": "audio-1",
            "asset_id": "new",
        }]},
    )))

    assert response.status == 200
    assert len(saves) == 1
    assert track.source_path == "media/new.wav"
    assert track.timeline_start_frame == 5
    assert track.timeline_end_frame == 23
    assert track.source_in_frame == 30
    assert track.total_source_frames == 48
    assert track.source_origin_frame == 0
    assert track.volume == 0.25
    assert track.muted is True
    assert track.lane_index == 3


def test_scene_mutation_replace_source_rejects_invalid_replacement_assets(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    clip = ClipReference(clip_id="clip-1", source_path="media/old.mp4", timeline_start_frame=0, timeline_end_frame=24)
    track = AudioTrack(track_id="audio-1", source_path="media/old.wav", timeline_start_frame=0, timeline_end_frame=24)
    scene.clips = [clip]
    scene.audio_tracks = [track]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    project.assets = [
        Asset(asset_id="image", asset_type="image", path="media/ref.png"),
        Asset(asset_id="trashed-video", asset_type="video", path="media/trashed.mp4", frame_count=30, trashed_at="2026-07-06T00:00:00"),
        Asset(asset_id="bad-audio", asset_type="audio", path="media/bad.wav", duration_sec=0.0),
    ]
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))
    handler = _mutation_handler(route_module)

    wrong_type = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{"type": "replace_clip_source", "clip_id": "clip-1", "asset_id": "image"}]},
    )))
    trashed = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{"type": "replace_clip_source", "clip_id": "clip-1", "asset_id": "trashed-video"}]},
    )))
    invalid_duration = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{"type": "replace_audio_source", "track_id": "audio-1", "asset_id": "bad-audio"}]},
    )))

    assert wrong_type.status == 400
    assert _response_json(wrong_type)["code"] == "invalid_source_asset"
    assert trashed.status == 409
    assert _response_json(trashed)["code"] == "asset_trashed"
    assert invalid_duration.status == 400
    assert _response_json(invalid_duration)["code"] == "invalid_source_asset"
    assert saves == []
    assert clip.source_path == "media/old.mp4"
    assert track.source_path == "media/old.wav"


def test_scene_mutation_update_clip_rejects_driver_lane_collision_atomically(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", motion_driver_lane_count=1)
    driver = ClipReference(
        clip_id="driver-1",
        source_path="media/driver-a.mp4",
        timeline_start_frame=0,
        timeline_end_frame=20,
        track_index=0,
        role="motion_driver",
    )
    render = ClipReference(
        clip_id="render-1",
        source_path="media/driver-b.mp4",
        timeline_start_frame=0,
        timeline_end_frame=20,
        track_index=0,
        role="render",
    )
    scene.clips = [driver, render]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    response = asyncio.run(_mutation_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_clip",
            "clip_id": "render-1",
            "fields": {"role": "motion_driver", "track_index": 0},
        }]},
    )))

    assert response.status == 409
    assert _response_json(response)["code"] == "driver_lane_occupied"
    assert saves == []
    assert render.role == "render"
    assert len([clip for clip in scene.clips if clip.role == "motion_driver"]) == 1


def test_scene_mutation_linked_split_rejects_driver_clip_atomically(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=40)
    driver = ClipReference(
        clip_id="driver-1",
        source_path="media/driver.mp4",
        timeline_start_frame=0,
        timeline_end_frame=20,
        source_in_frame=0,
        source_out_frame=20,
        total_source_frames=20,
        track_index=0,
        role="motion_driver",
    )
    audio = AudioTrack(
        track_id="audio-1",
        source_path="media/audio.wav",
        timeline_start_frame=0,
        timeline_end_frame=20,
        source_in_frame=0,
        total_source_frames=20,
    )
    scene.clips = [driver]
    scene.audio_tracks = [audio]
    scene.linked_item_groups = [{
        "group_id": "group-1",
        "items": [{"type": "clip", "id": "driver-1"}, {"type": "audio", "id": "audio-1"}],
    }]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    response = asyncio.run(_mutation_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "split_clip",
            "clip_id": "driver-1",
            "frame": 10,
            "apply_linked": True,
        }]},
    )))

    assert response.status == 409
    assert _response_json(response)["code"] == "driver_clip_split_refused"
    assert saves == []
    assert len(scene.clips) == 1
    assert len(scene.audio_tracks) == 1
    assert driver.timeline_end_frame == 20
    assert audio.timeline_end_frame == 20


def test_scene_mutation_create_prompt_section_returns_reconciled_scene(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={
            "operations": [{
                "type": "create_prompt_section",
                "fields": {"start_frame": 10, "end_frame": 20, "prompt": "hello"},
            }],
        },
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert len(saves) == 1
    assert len(payload["scene"]["prompt_sections"]) == 1
    section = payload["scene"]["prompt_sections"][0]
    assert section["start_frame"] == 10
    assert section["end_frame"] == 20
    assert section["channels"] == {"visual": "hello", "speech": "", "sounds": ""}
    assert section["prompt"] == "hello"
    assert section["muted"] is False
    assert section["prompt_id"]


def _mutation_handler(route_module):
    return _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )


def _prompt_scene_project(monkeypatch, route_module, tmp_path, sections):
    from server.timeline_state import PromptSection

    scene = Scene(scene_id="scene-1", name="Scene")
    scene.prompt_sections = [PromptSection(**fields) for fields in sections]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))
    return scene, project, saves


def test_scene_mutation_prompt_overlap_rejected(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene, _project, saves = _prompt_scene_project(
        monkeypatch, route_module, tmp_path,
        [{"start_frame": 0, "end_frame": 20, "prompt": "a"}],
    )
    handler = _mutation_handler(route_module)

    # Overlapping create → 409 prompt_overlap, nothing saved
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "create_prompt_section",
            "fields": {"start_frame": 10, "end_frame": 30, "prompt": "b"},
        }]},
    )))
    assert response.status == 409
    assert _response_json(response)["code"] == "prompt_overlap"
    assert saves == []
    assert len(scene.prompt_sections) == 1

    # Abutting create (half-open) is fine
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "create_prompt_section",
            "fields": {"start_frame": 20, "end_frame": 30, "prompt": "b"},
        }]},
    )))
    assert response.status == 200
    assert len(scene.prompt_sections) == 2

    # Range update creating an overlap → 409; text-only update on the same
    # section passes (legacy stored overlaps stay editable)
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_prompt_section",
            "index": 1,
            "fields": {"start_frame": 15},
            "expected": {"start_frame": 20, "end_frame": 30},
        }]},
    )))
    assert response.status == 409
    assert _response_json(response)["code"] == "prompt_overlap"

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_prompt_section",
            "index": 1,
            "fields": {"channels": {"visual": "updated", "speech": "say", "sounds": ""}},
            "expected": {"start_frame": 20, "end_frame": 30},
        }]},
    )))
    assert response.status == 200
    assert scene.prompt_sections[1].channels == {"visual": "updated", "speech": "say", "sounds": ""}


def test_scene_mutation_swap_prompt_sections_atomic(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene, _project, saves = _prompt_scene_project(
        monkeypatch, route_module, tmp_path,
        [
            {"start_frame": 0, "end_frame": 10, "prompt": "first"},
            {"start_frame": 40, "end_frame": 80, "prompt": "second"},
        ],
    )
    handler = _mutation_handler(route_module)

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "swap_prompt_sections",
            "index_a": 0,
            "index_b": 1,
            "expected_a": {"start_frame": 0, "end_frame": 10},
            "expected_b": {"start_frame": 40, "end_frame": 80},
            # Exact previewed final ranges (frontend computes the preview)
            "fields_a": {"start_frame": 40, "end_frame": 50},
            "fields_b": {"start_frame": 0, "end_frame": 40},
        }]},
    )))
    assert response.status == 200
    assert len(saves) == 1
    # Array re-sorted by start: "second" now leads, "first" follows — no
    # stale-index intermediate state ever existed
    assert [(s.prompt, s.start_frame, s.end_frame) for s in scene.prompt_sections] == [
        ("second", 0, 40),
        ("first", 40, 50),
    ]

    # Overlapping final state → 409, untouched
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "swap_prompt_sections",
            "index_a": 0,
            "index_b": 1,
            "fields_a": {"start_frame": 0, "end_frame": 45},
            "fields_b": {"start_frame": 40, "end_frame": 50},
        }]},
    )))
    assert response.status == 409
    assert _response_json(response)["code"] == "prompt_overlap"


def test_scene_mutation_global_prompt_lock_guard(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene, _project, saves = _prompt_scene_project(monkeypatch, route_module, tmp_path, [])
    scene.global_prompt_track_config = LaneConfig(locked=True)
    handler = _mutation_handler(route_module)

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_scene_fields",
            "fields": {"prompt": "new global"},
        }]},
    )))
    assert response.status == 409
    assert _response_json(response)["code"] == "track_locked"
    assert saves == []
    assert scene.prompt == ""

    # global_prompt_track_config persists through update_lane_configs
    scene.global_prompt_track_config = LaneConfig()
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_lane_configs",
            "fields": {"global_prompt_track_config": {"hidden": True, "locked": False}},
        }]},
    )))
    assert response.status == 200
    assert scene.global_prompt_track_config.hidden is True


def test_queue_route_constructor_carries_scene_prompt(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path), name="Project")
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project, **kwargs: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/queue",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        body={
            "scene_id": "scene-1",
            "selection_start": 0,
            "selection_end": 16,
            "prompt": "global text [VISUAL]: action",
            "scene_prompt": "global text",
            "prompt_sections": [
                {"start_frame": 0, "end_frame": 16,
                 "channels": {"visual": "action", "speech": "", "sounds": ""},
                 "prompt": "action"},
            ],
        },
    )))
    assert response.status == 200
    job = project.generation_queue[0]
    # The route constructor allowlist must carry the frozen global text and
    # flag the snapshot version — not just GenerationJob.from_dict
    assert job.scene_prompt == "global text"
    assert job.params.get("snapshot_version") == 1
    assert job.prompt_sections[0]["channels"]["visual"] == "action"
    # Prompt Saver: history captured in the SAME save as the enqueue
    assert len(saves) == 1
    history = project.metadata.get("prompt_history")
    assert len(history) == 1
    assert history[0]["global"] == "global text"
    assert history[0]["sections"][0]["channels"]["visual"] == "action"


def test_queue_route_composes_frozen_prompt_server_side(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    # No matching scene on the project — compose must come from job fields +
    # project metadata only (the frozen envelope is the authority)
    project = TimelineProject(project_dir=str(tmp_path), name="Project")
    project.metadata["prompt_section_delimiter"] = ","
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project, **kwargs: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/queue",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        body={
            "scene_id": "scene-1",
            "selection_start": 10,
            "selection_end": 60,
            "pre_context_frames": 5,
            "post_context_frames": 5,
            "prompt": "CLIENT DISPLAY VALUE",
            "scene_prompt": "global",
            "prompt_sections": [
                {"start_frame": 0, "end_frame": 30,
                 "channels": {"visual": "first", "speech": "", "sounds": ""}},
                {"start_frame": 30, "end_frame": 80,
                 "channels": {"visual": "second", "speech": "", "sounds": ""}},
                # Section entirely outside the raw window [5, 65) — the
                # window-drift pin (audit F3): just-outside is absent
                {"start_frame": 90, "end_frame": 100,
                 "channels": {"visual": "outside", "speech": "", "sounds": ""}},
            ],
        },
    )))
    assert response.status == 200
    job = project.generation_queue[0]
    # Server-side override: multi-segment compose with the project delimiter
    # and default labels-off behavior, NOT the client display value
    assert job.prompt == "global first, second"
    assert job.params["prompt_section_delimiter"] == ","
    # Legacy non-snapshot body keeps the client value
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        body={"scene_id": "scene-1", "selection_start": 0, "selection_end": 16,
              "prompt": "legacy client prompt"},
    )))
    assert response.status == 200
    assert project.generation_queue[1].prompt == "legacy client prompt"


def test_queue_batch_chunks_get_differing_composed_prompts(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path), name="Project")
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project, **kwargs: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/queue/batch",
    )
    # Two chunks whose windows cross DIFFERENT sections (per-chunk freezing:
    # each chunk's snapshot carries only its window-overlapping sections)
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        body={"jobs": [
            {
                "scene_id": "scene-1", "selection_start": 0, "selection_end": 40,
                "batch_id": "b1", "batch_total": 2, "batch_index": 0,
                "scene_prompt": "g",
                "prompt_sections": [
                    {"start_frame": 0, "end_frame": 40,
                     "channels": {"visual": "chunk one action", "speech": "", "sounds": ""}},
                ],
            },
            {
                "scene_id": "scene-1", "selection_start": 40, "selection_end": 80,
                "batch_id": "b1", "batch_total": 2, "batch_index": 1,
                "scene_prompt": "g",
                "prompt_sections": [
                    {"start_frame": 40, "end_frame": 80,
                     "channels": {"visual": "chunk two action", "speech": "", "sounds": ""}},
                ],
            },
        ]},
    )))
    assert response.status == 201
    assert len(saves) == 1
    prompts = [job.prompt for job in project.generation_queue]
    assert prompts == ["g chunk one action", "g chunk two action"]


def test_queue_prompt_history_dedup_and_cap(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path), name="Project")
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project, **kwargs: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/queue",
    )

    def enqueue(prompt_text):
        return asyncio.run(handler(DummyRequest(
            match_info={"project_id": "proj"},
            body={
                "scene_id": "scene-1",
                "selection_start": 0,
                "selection_end": 16,
                "scene_prompt": "global",
                "prompt_sections": [
                    {"start_frame": 0, "end_frame": 16,
                     "channels": {"visual": prompt_text, "speech": "", "sounds": ""}},
                ],
            },
        )))

    # Same payload twice → one entry, ts bumped; new payload → second entry
    enqueue("same")
    first_ts = project.metadata["prompt_history"][0]["ts"]
    enqueue("same")
    assert len(project.metadata["prompt_history"]) == 1
    assert project.metadata["prompt_history"][0]["ts"] >= first_ts
    enqueue("different")
    assert len(project.metadata["prompt_history"]) == 2

    # Cap: newest entries survive
    cap = route_module.PROMPT_HISTORY_CAP
    for i in range(cap + 5):
        enqueue(f"prompt {i}")
    history = project.metadata["prompt_history"]
    assert len(history) == cap
    assert any(f"prompt {cap + 4}" in (e["sections"][0]["channels"]["visual"]) for e in history[-1:])


def test_queue_batch_route_appends_all_jobs_with_single_save(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path), name="Project")
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project, **kwargs: saves.append(saved_project))

    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/queue/batch",
    )
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        body={
            "jobs": [
                {
                    "scene_id": "scene-1",
                    "selection_start": 0,
                    "selection_end": 16,
                    "batch_id": "batch-1",
                    "batch_total": 2,
                    "batch_index": 0,
                    "template_id": "ltx-2.3",
                    "frame_constraint": {"step": 8, "offset": 1},
                },
                {
                    "scene_id": "scene-1",
                    "selection_start": 16,
                    "selection_end": 32,
                    "batch_id": "batch-1",
                    "batch_total": 2,
                    "batch_index": 1,
                    "template_id": "ltx-2.3",
                    "frame_constraint": {"step": 8, "offset": 1},
                },
            ],
        },
    )))
    payload = _response_json(response)

    assert response.status == 201
    assert payload["count"] == 2
    assert len(payload["jobs"]) == 2
    assert len(project.generation_queue) == 2
    assert len(saves) == 1
    assert [job.batch_index for job in project.generation_queue] == [0, 1]
    assert all(job.frame_constraint == {"step": 8, "offset": 1} for job in project.generation_queue)


def _queue_mutations_handler(route_module):
    return _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/queue/mutations",
    )


def _queue_ids(project):
    return [job.job_id for job in project.generation_queue]


def test_queue_mutation_delete_absent_job_is_noop(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path), name="Project")
    project.generation_queue = [GenerationJob(job_id="job-a"), GenerationJob(job_id="job-b")]
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project, **kwargs: saves.append(saved_project))

    response = asyncio.run(_queue_mutations_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj"},
        body={"operations": [{"type": "delete_job", "job_id": "missing"}]},
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert _queue_ids(project) == ["job-a", "job-b"]
    assert [job["job_id"] for job in payload["queue"]] == ["job-a", "job-b"]
    assert payload["results"][0]["removed"] == 0
    assert saves == []


def test_legacy_queue_delete_absent_job_is_idempotent(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path), name="Project")
    project.generation_queue = [GenerationJob(job_id="job-a")]
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project, **kwargs: saves.append(saved_project))

    handler = _route_handler(route_module, "DELETE", "/sonder-editor/project/{project_id}/queue/{job_id}")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "job_id": "missing"},
        method="DELETE",
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["removed"] == 0
    assert _queue_ids(project) == ["job-a"]
    assert saves == []


def test_queue_mutations_clear_completed_and_clear_all(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir=str(tmp_path), name="Project")
    project.generation_queue = [
        GenerationJob(job_id="pending", status="pending"),
        GenerationJob(job_id="done", status="completed"),
        GenerationJob(job_id="running", status="running"),
    ]
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project, **kwargs: saves.append(saved_project))
    handler = _queue_mutations_handler(route_module)

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        body={"operations": [{"type": "clear_completed"}]},
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["results"][0]["removed"] == 1
    assert _queue_ids(project) == ["pending", "running"]

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        body={"operations": [{"type": "clear_all"}]},
    )))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["results"][0]["removed"] == 2
    assert project.generation_queue == []
    assert len(saves) == 2


def test_queue_mutations_retry_stale_deletes_without_resurrection(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = TimelineProject(project_dir=str(project_dir), name="Project")
    project.project_id = "proj"
    project.generation_queue = [
        GenerationJob(job_id="job-a"),
        GenerationJob(job_id="job-b"),
        GenerationJob(job_id="job-c"),
    ]
    route_module.save_project(project)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    base_version = project.modified_at
    handler = _queue_mutations_handler(route_module)

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        headers={"If-Match": base_version},
        method="POST",
        body={"operations": [{"type": "delete_job", "job_id": "job-a"}]},
    )))
    assert response.status == 200

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        headers={"If-Match": base_version},
        method="POST",
        body={"operations": [{"type": "delete_job", "job_id": "job-b"}]},
    )))
    payload = _response_json(response)
    restored = route_module.load_project(str(project_dir))

    assert response.status == 200
    assert [job["job_id"] for job in payload["queue"]] == ["job-c"]
    assert _queue_ids(restored) == ["job-c"]


def test_queue_update_retries_stale_version_and_preserves_newer_jobs(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = TimelineProject(project_dir=str(project_dir), name="Project")
    project.project_id = "proj"
    project.generation_queue = [GenerationJob(job_id="job-a", status="pending")]
    route_module.save_project(project)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    base_version = project.modified_at

    current = route_module.load_project(str(project_dir))
    current.generation_queue.append(GenerationJob(job_id="job-b", status="pending"))
    route_module.save_project(current)

    handler = _route_handler(route_module, "PUT", "/sonder-editor/project/{project_id}/queue/{job_id}")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "job_id": "job-a"},
        headers={"If-Match": base_version},
        method="PUT",
        body={"status": "completed", "progress": 1.0},
    )))
    payload = _response_json(response)
    restored = route_module.load_project(str(project_dir))

    assert response.status == 200
    assert payload["status"] == "completed"
    assert {job.job_id: job.status for job in restored.generation_queue} == {
        "job-a": "completed",
        "job-b": "pending",
    }


def _mutations_handler(route_module):
    return _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )


def _lane_config_project(tmp_path):
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig(name="V0", color="#123", locked=False, hidden=True)]
    scene.audio_lane_count = 2
    scene.audio_lane_configs = [LaneConfig(name="A0"), LaneConfig(name="A1", locked=True)]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    return project, scene


def test_scene_mutation_update_lane_config_partial_fields(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project, scene = _lane_config_project(tmp_path)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    response = asyncio.run(_mutations_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_lane_config",
            "lane_type": "video",
            "lane_index": 0,
            "fields": {"locked": True},
        }]},
    )))

    assert response.status == 200
    assert len(saves) == 1
    config = scene.video_lane_configs[0]
    assert config.locked is True
    # Partial update: untouched fields survive
    assert config.name == "V0"
    assert config.color == "#123"
    assert config.hidden is True


def test_scene_mutation_update_lane_config_pads_short_config_list(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project, scene = _lane_config_project(tmp_path)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    # lane_count is 3 but the config list only has one entry: index 2 is a
    # legal lane whose config must be padded into existence.
    response = asyncio.run(_mutations_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_lane_config",
            "lane_type": "video",
            "lane_index": 2,
            "fields": {"hidden": True},
        }]},
    )))

    assert response.status == 200
    assert len(scene.video_lane_configs) >= 3
    assert scene.video_lane_configs[2].hidden is True
    assert scene.video_lane_configs[0].name == "V0"


def test_scene_mutation_update_lane_config_index_beyond_count_rejects(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project, scene = _lane_config_project(tmp_path)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    response = asyncio.run(_mutations_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_lane_config",
            "lane_type": "audio",
            "lane_index": 2,
            "fields": {"locked": True},
        }]},
    )))

    assert response.status == 404
    assert _response_json(response)["code"] == "item_not_found"
    assert saves == []


def test_scene_mutation_update_lane_config_fixed_tracks(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project, scene = _lane_config_project(tmp_path)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    response = asyncio.run(_mutations_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [
            {"type": "update_lane_config", "lane_type": "guide", "fields": {"locked": True}},
            {"type": "update_lane_config", "lane_type": "prompt_global", "fields": {"hidden": True}},
        ]},
    )))

    assert response.status == 200
    assert len(saves) == 1
    assert scene.guide_track_config.locked is True
    assert scene.global_prompt_track_config.hidden is True


def test_scene_mutation_update_lane_config_unlocks_locked_lane(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project, scene = _lane_config_project(tmp_path)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    # No lock gate: toggling `locked` itself must work on a locked lane.
    response = asyncio.run(_mutations_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_lane_config",
            "lane_type": "audio",
            "lane_index": 1,
            "fields": {"locked": False},
        }]},
    )))

    assert response.status == 200
    assert scene.audio_lane_configs[1].locked is False


def test_scene_mutation_update_lane_config_unknown_lane_type_rejects(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project, scene = _lane_config_project(tmp_path)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    response = asyncio.run(_mutations_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [{
            "type": "update_lane_config",
            "lane_type": "lanes",
            "lane_index": 0,
            "fields": {"locked": True},
        }]},
    )))

    assert response.status == 400
    assert saves == []


def test_scene_mutation_update_lane_config_multi_op_single_save(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project, scene = _lane_config_project(tmp_path)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))

    # Bulk header apply: several lanes toggled in ONE mutation batch/save.
    response = asyncio.run(_mutations_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": [
            {"type": "update_lane_config", "lane_type": "video", "lane_index": 0, "fields": {"locked": True}},
            {"type": "update_lane_config", "lane_type": "video", "lane_index": 1, "fields": {"locked": True}},
            {"type": "update_lane_config", "lane_type": "audio", "lane_index": 0, "fields": {"locked": True}},
        ]},
    )))

    assert response.status == 200
    assert len(saves) == 1
    assert scene.video_lane_configs[0].locked is True
    assert scene.video_lane_configs[1].locked is True
    assert scene.audio_lane_configs[0].locked is True


def test_load_project_repair_save_is_unversioned_and_non_fatal(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.clips = [ClipReference(
        clip_id="clip-0", timeline_start_frame=0, timeline_end_frame=10,
        source_out_frame=10, total_source_frames=0, track_index=0,
    )]
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.project_id = "proj"
    route_module.save_project(project)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(route_module, "load_project", lambda project_dir: project)
    save_calls = []

    def contended_save(saved_project, **kwargs):
        save_calls.append(kwargs)
        raise PermissionError("locked by another writer")

    monkeypatch.setattr(route_module, "save_project", contended_save)

    request = DummyRequest(
        match_info={"project_id": "proj"},
        method="GET",
        path="/sonder-editor/project/proj/scenes",
    )
    loaded = route_module._load_project_from_request(request)

    # The repair is applied in memory and served despite the contended save,
    # and the save is a no-bump/no-broadcast back-fill.
    assert loaded is project
    assert scene.clips[0].total_source_frames == 10
    assert save_calls == [{"bump_modified_at": False, "notify": False}]


def test_scenes_get_serves_despite_contended_repair_save(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.clips = [ClipReference(
        clip_id="clip-0", timeline_start_frame=0, timeline_end_frame=10,
        source_out_frame=10, total_source_frames=0, track_index=0,
    )]
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = TimelineProject(project_dir=str(project_dir), name="Project", scenes=[scene])
    project.project_id = "proj"
    route_module.save_project(project)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(route_module, "load_project", lambda project_dir: project)

    def contended_save(saved_project, **kwargs):
        raise PermissionError("locked by another writer")

    monkeypatch.setattr(route_module, "save_project", contended_save)

    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/scenes")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj"},
        method="GET",
        path="/sonder-editor/project/proj/scenes",
    )))

    assert response.status == 200
    payload = _response_json(response)
    assert payload["scenes"][0]["scene_id"] == "scene-1"
