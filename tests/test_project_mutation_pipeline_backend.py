import asyncio
import copy
import importlib
import json
import os
import sys
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import server.routes as routes
from server import prompt_context
from server.timeline_state import Asset, AudioTrack, ClipReference, GenerationJob, GuideFrame, LaneConfig, PromptSection, ReferenceEntity, ReferenceItem, ReferenceLaneRecipe, ReferenceMember, Scene, TimelineProject


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


def test_prompt_split_preserves_global_channel_exclusions():
    scene = Scene(scene_id="scene", duration_frames=20)
    section = PromptSection(
        0, 20, channels={"visual": "one"},
        global_channel_exceptions=["speech", "sounds"])
    scene.prompt_sections = [section]
    right = routes._split_prompt_object(scene, section, 10)
    assert set(right.global_channel_exceptions) == {"speech", "sounds"}


def test_direct_section_route_returns_structured_conflict_without_partial_mutation(
        monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    attachment = prompt_context.normalize_attachment({"kind": "guide"})
    section = PromptSection(
        0, 20, channels={"visual": "before"}, attachments=[attachment],
        channel_docs={"visual": {"nodes": [
            {"type": "text", "node_id": "text", "text": "before"},
            {"type": "attachment", "node_id": "anchor",
             "attachment_id": attachment["attachment_id"]},
        ]}},
    )
    scene = Scene(scene_id="scene", duration_frames=20,
                  prompt_sections=[section])
    project = TimelineProject(project_dir=str(tmp_path), project_id="proj",
                              scenes=[scene])
    before = section.to_dict()
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda saved, **kwargs: saves.append(saved))
    handler = _route_handler(
        route_module, "PUT",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt_sections/{index}")

    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene", "index": "0"},
        body={"start_frame": 1, "channels": {"visual": "replacement"}},
        method="PUT",
    )))

    assert response.status == 409
    assert _response_json(response)["code"] == "structured_edit_conflict"
    assert section.to_dict() == before
    assert saves == []


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


def test_prompt_history_route_reads_component_without_loading_model(monkeypatch, tmp_path):
    from server.project_manager import create_project, save_project
    route_module = _load_route_module(monkeypatch)
    project = create_project("History endpoint", base_dir=str(tmp_path))
    history = [{"hash": "test", "sections": [{"channels": {"visual": "frozen"}}]}]
    project.metadata["prompt_history"] = copy.deepcopy(history)
    save_project(project, notify=False)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    def forbidden(*args, **kwargs):
        raise AssertionError("History endpoint must not load a project model")
    monkeypatch.setattr(route_module, "_load_project_from_request", forbidden)
    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/prompt-history")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": os.path.basename(project.project_dir)}, method="GET")))
    assert response.status == 200
    assert _response_json(response) == history


def _response_json(response):
    return json.loads(response.body.decode("utf-8"))


def test_prompt_context_candidate_uses_constraint_aware_execution_window(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", duration_frames=100)
    scene.prompt_sections = [PromptSection(
        0, 100, channels={"visual": "body"})]
    project = TimelineProject(project_id="proj", scenes=[scene], fps=24.0)
    monkeypatch.setattr(
        route_module, "_load_project_from_request",
        lambda request, **_kwargs: project)
    handler = _route_handler(
        route_module, "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-context/compile")
    body = {
        "scene": scene.to_dict(),
        "selection_start": 30, "selection_end": 50,
        "pre_context_frames": 6, "post_context_frames": 6,
        "mask_pre_offset": 2, "mask_post_offset": 2,
        "frame_constraint": {"step": 17, "offset": 5, "min": 5},
    }
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene"}, body=body)))
    payload = _response_json(response)
    expected = route_module.resolve_execution_window(
        scene_duration=100, selection_start=30, selection_end=50,
        pre_context_frames=6, post_context_frames=6,
        mask_pre_offset=2, mask_post_offset=2,
        frame_constraint={"step": 17, "offset": 5, "min": 5})
    assert payload["execution_window"] == expected
    assert payload["window"]["start_frame"] == expected["render_start"]
    assert payload["window"]["end_frame"] == expected["render_end"]


def test_mutation_route_entry_is_preparse_gated_and_bounded(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    events = []
    monkeypatch.setattr(
        route_module, "record_diag_event",
        lambda event, **payload: events.append((event, payload)))

    class MalformedRequest(DummyRequest):
        async def json(self):
            raise json.JSONDecodeError("malformed", "{", 1)

    async def malformed_handler(request):
        assert len(events) == 1
        try:
            await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        raise AssertionError("Malformed JSON unexpectedly parsed")

    long_gesture_id = "g" * 300
    request = MalformedRequest(
        match_info={"project_id": "folder-project"},
        method="POST",
        path="/api/sonder-editor/project/folder-project/scenes/scene/mutations",
        headers={
            "X-Sonder-Gesture-Id": long_gesture_id,
            "X-Sonder-Gesture-Kind": "moveItem",
            "X-Sonder-Request-Id": "mutation-7",
            "X-Sonder-Gesture-Attempt": "2",
            "X-Sonder-Mutation-Coalesced-Count": "3",
        },
    )
    response = asyncio.run(route_module._route_timing_middleware(
        request, malformed_handler))

    assert response.status == 400
    assert events == [("mutation_route_entry", {
        "project_id": "folder-project",
        "path": "/api/sonder-editor/project/folder-project/scenes/scene/mutations",
        "method": "POST",
        "gesture_id": "g" * 256,
        "gesture_kind": "moveItem",
        "request_id": "mutation-7",
        "attempt": "2",
        "coalesced_count": "3",
    })]

    events.clear()
    unscoped_request = DummyRequest(
        match_info={"project_id": "folder-project"},
        method="POST",
        path="/api/sonder-editor/project/folder-project/scenes/scene/mutations")
    asyncio.run(route_module._route_timing_middleware(
        unscoped_request, lambda _request: asyncio.sleep(
            0, result=web.json_response({"ok": True}))))
    assert events == [("mutation_route_entry", {
        "project_id": "folder-project",
        "path": "/api/sonder-editor/project/folder-project/scenes/scene/mutations",
        "method": "POST",
        "gesture_id": "",
        "gesture_kind": "unscoped",
        "request_id": "",
        "attempt": "",
        "coalesced_count": "",
    })]

    events.clear()
    for read_shaped_path in (
        "/api/sonder-editor/session/folder-project/heartbeat",
        "/api/sonder-editor/project/folder-project/scenes/scene/prompt-context/compile",
        "/api/sonder-editor/project/folder-project/reveal",
        "/api/sonder-editor/project/folder-project/assets/bulk-usages",
    ):
        read_shaped_request = DummyRequest(
            match_info={"project_id": "folder-project"},
            method="POST",
            path=read_shaped_path)
        asyncio.run(route_module._route_timing_middleware(
            read_shaped_request, lambda _request: asyncio.sleep(
                0, result=web.json_response({"ok": True}))))
    assert events == []

    events.clear()
    get_request = DummyRequest(
        match_info={"project_id": "folder-project"},
        method="GET",
        path="/api/sonder-editor/project/folder-project/scenes")
    asyncio.run(route_module._route_timing_middleware(
        get_request, lambda _request: asyncio.sleep(
            0, result=web.json_response({"ok": True}))))
    assert events == []


def test_prompt_context_candidate_cpu_helper_is_off_loop_and_route_entry_is_countable(
        monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", duration_frames=24)
    project = TimelineProject(project_id="proj", scenes=[scene], fps=24.0)
    monkeypatch.setattr(
        route_module, "_load_project_from_request",
        lambda request, **_kwargs: project)
    events = []
    monkeypatch.setattr(
        route_module, "record_diag_event",
        lambda event, **payload: events.append((event, payload)))
    release = threading.Event()
    worker_threads = []

    def blocked_compile(value, scene_id, body):
        worker_threads.append(threading.get_ident())
        assert value is project
        assert scene_id == "scene"
        assert body == {"labels_on": True}
        assert release.wait(1.0)
        return 200, {"ok": True}

    monkeypatch.setattr(
        route_module, "_compile_prompt_context_candidate_sync", blocked_compile)
    handler = _route_handler(
        route_module, "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-context/compile")
    request = DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene"},
        body={"labels_on": True},
        headers={
            "X-Sonder-Prompt-Purpose": "windowed-preview",
            "X-Sonder-Prompt-Request-Id": "prompt-7",
            "X-Sonder-Prompt-Attempt": "1",
        })

    async def exercise():
        loop = asyncio.get_running_loop()
        main_thread = threading.get_ident()
        loop.call_later(0.02, release.set)
        started = loop.time()
        response = await handler(request)
        return response, loop.time() - started, main_thread

    response, elapsed, main_thread = asyncio.run(exercise())

    assert response.status == 200
    assert _response_json(response) == {"ok": True}
    assert elapsed < 0.5
    assert worker_threads and worker_threads[0] != main_thread
    assert events == [("prompt_context_compile_route_entry", {
        "project_id": "proj",
        "scene_id": "scene",
        "purpose": "windowed-preview",
        "request_id": "prompt-7",
        "attempt": "1",
    })]


def test_prompt_context_candidate_compiles_transient_pending_identity_without_mutation(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    event = prompt_context.normalize_attachment({
        "attachment_id": "vocal-1", "kind": "vocal_event",
        "source": {"subject_ids": ["pending-1"]},
        "config": {"event_type": "dialogue", "language": "English",
                   "text": "Hello there."},
    })
    scene = Scene(scene_id="scene", duration_frames=24,
                  prompt_context_profile_id="minimax_h3_ref@1")
    scene.prompt_sections = [PromptSection(
        0, 24, attachments=[event], channel_docs={"detailed_description": {
            "nodes": [{"type": "attachment", "node_id": "anchor",
                       "attachment_id": "vocal-1"}],
        }})]
    project = TimelineProject(project_id="proj", scenes=[scene], fps=24.0)
    project.metadata["prompt_channel_template"] = "minimax_h3_ref"
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **_kwargs: project)
    handler = _route_handler(
        route_module, "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-context/compile")
    body = {
        "scene": scene.to_dict(),
        "channel_template": "minimax_h3_ref",
        "selection_start": 0, "selection_end": 24,
        "prompt_semantic_unit_creates": [{
            "type": "create_prompt_semantic_unit",
            "handle_suggestion": "Narrator",
            "unit": {"semantic_unit_id": "pending-1", "name": "Narrator",
                     "kind": "subject", "definition": "A calm narrator"},
        }],
    }
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene"}, body=body)))
    payload = _response_json(response)

    assert response.status == 200
    assert "broken_vocal_identity" not in {
        value["code"] for value in payload["errors"]}
    assert "A calm narrator" in payload["prompt"]
    assert project.prompt_semantic_units == []


def test_prompt_context_candidate_pending_identity_collision_refuses(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", duration_frames=24,
                  prompt_context_profile_id="minimax_h3_ref@1")
    project = TimelineProject(project_id="proj", scenes=[scene], fps=24.0)
    project.metadata["prompt_channel_template"] = "minimax_h3_ref"
    project.prompt_semantic_units = [prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "pending-1", "name": "Existing",
        "handle": "Existing", "kind": "subject", "definition": "Existing",
    })]
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **_kwargs: project)
    handler = _route_handler(
        route_module, "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-context/compile")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene"}, body={
            "scene": scene.to_dict(), "channel_template": "minimax_h3_ref",
            "prompt_semantic_unit_creates": [{
                "type": "create_prompt_semantic_unit",
                "unit": {"semantic_unit_id": "pending-1", "name": "Different",
                         "kind": "subject"},
            }],
        })))

    assert response.status == 409
    assert _response_json(response)["code"] == "prompt_semantic_unit_collision"
    assert project.prompt_semantic_units[0]["name"] == "Existing"


def test_prompt_context_candidate_pending_identity_overlay_is_bounded(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", duration_frames=24)
    project = TimelineProject(project_id="proj", scenes=[scene], fps=24.0)
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **_kwargs: project)
    handler = _route_handler(
        route_module, "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-context/compile")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene"}, body={
            "scene": scene.to_dict(),
            "prompt_semantic_unit_creates": [
                {"type": "create_prompt_semantic_unit",
                 "unit": {"semantic_unit_id": f"pending-{index}",
                          "name": f"Speaker {index}", "kind": "subject"}}
                for index in range(65)
            ],
        })))

    assert response.status == 400
    assert _response_json(response)["code"] == "prompt_semantic_unit_overlay_too_large"
    assert project.prompt_semantic_units == []


def test_prompt_context_candidate_create_and_compile_share_requested_template(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", duration_frames=24)
    project = TimelineProject(project_id="proj", scenes=[scene], fps=24.0)
    monkeypatch.setattr(route_module, "_load_project_from_request",
                        lambda request, **_kwargs: project)
    seen = {}

    def apply_create(_project, _scene, _op, *, template=None):
        seen["create"] = template
        return {"created": True}

    def compile_candidate(_project, _scene, *, template=None, **_kwargs):
        seen["compile"] = template
        return {"prompt": "", "errors": []}

    monkeypatch.setattr(route_module, "_apply_create_prompt_semantic_unit", apply_create)
    monkeypatch.setattr(route_module, "compile_live_scene_prompt_context", compile_candidate)
    handler = _route_handler(
        route_module, "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-context/compile")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene"}, body={
            "scene": scene.to_dict(), "channel_template": "standard",
            "prompt_semantic_unit_creates": [{
                "type": "create_prompt_semantic_unit",
                "unit": {"semantic_unit_id": "pending", "name": "Narrator",
                         "kind": "subject"},
            }],
        })))

    assert response.status == 200
    assert seen["create"] is seen["compile"]
    assert seen["compile"]["id"] == "standard"


def _apply_scene_operations(route_module, monkeypatch, project, scene_id, operations, saves):
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project", lambda saved_project: saves.append(saved_project))
    handler = _route_handler(
        route_module,
        "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations",
    )
    return asyncio.run(handler(DummyRequest(
        match_info={"project_id": "proj", "scene_id": scene_id},
        body={"operations": operations},
    )))


def test_create_clip_batch_adds_tail_lane_audio_link_and_saves_once(
        monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", video_lane_count=1, audio_lane_count=1)
    video = Asset(
        asset_id="video", asset_type="video", path="media/video.mp4",
        frame_count=24, duration_sec=1.0, has_audio=True)
    audio = Asset(
        asset_id="derived", asset_type="audio", path="media/video_audio.wav",
        duration_sec=1.0, duration_checked=True)
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="proj", scenes=[scene],
        assets=[video])
    saves = []
    monkeypatch.setattr(
        route_module, "_prepare_video_audio_asset", lambda *_args: audio)

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "set_lane_count", "lane_type": "video", "count": 2},
        {"type": "set_lane_count", "lane_type": "audio", "count": 2},
        {"type": "create_clip", "fields": {
            "asset_id": "video", "timeline_start_frame": 5,
            "track_index": 1, "audio_lane_index": 1, "dual_drop": True,
            "link_video_audio": True,
        }},
    ], saves)
    payload = _response_json(response)
    result = payload["results"][2]

    assert response.status == 200
    assert len(saves) == 1
    assert payload["operation_count"] == 3
    assert result["type"] == "create_clip"
    assert result["clip"]["track_index"] == 1
    assert result["audio_track"]["lane_index"] == 1
    assert result["audio_asset"]["asset_id"] == "derived"
    assert payload["scene"]["video_lane_count"] == 2
    assert payload["scene"]["audio_lane_count"] == 2
    group = payload["scene"]["linked_item_groups"][0]
    assert not group["group_id"].startswith("temp-drop-")
    assert {item["type"] for item in group["items"]} == {"clip", "audio"}


def test_create_clip_dual_drop_keeps_clip_when_extraction_fails(
        monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene")
    video = Asset(
        asset_id="video", asset_type="video", path="media/video.mp4",
        frame_count=24, duration_sec=1.0, has_audio=True)
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="proj", scenes=[scene],
        assets=[video])
    saves = []
    monkeypatch.setattr(
        route_module, "_prepare_video_audio_asset",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("ffmpeg failed")))

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [{
        "type": "create_clip", "fields": {
            "asset_id": "video", "dual_drop": True,
        },
    }], saves)
    result = _response_json(response)["results"][0]

    assert response.status == 200
    assert len(saves) == 1
    assert result["clip"]["clip_id"]
    assert result["audio_track"] is None
    assert result["audio_asset"] is None
    assert len(scene.clips) == 1
    assert scene.audio_tracks == []


@pytest.mark.parametrize(("fields", "status", "code"), [
    ({"asset_id": "missing"}, 404, "asset_not_found"),
    ({"asset_id": "video", "role": "invalid"}, 400, "invalid_clip_role"),
    ({"asset_id": "video", "fit_mode": "invalid"}, 400, "invalid_fit_mode"),
    ({"asset_id": "video", "crop_position": "invalid"}, 400,
     "invalid_crop_position"),
])
def test_create_clip_reports_stable_refusal_codes(
        monkeypatch, tmp_path, fields, status, code):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene")
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="proj", scenes=[scene],
        assets=[Asset(
            asset_id="video", asset_type="video", path="media/video.mp4",
            frame_count=24, duration_sec=1.0)])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "create_clip", "fields": fields},
    ], saves)

    assert response.status == status
    assert _response_json(response)["code"] == code
    assert scene.clips == []
    assert saves == []


def test_media_create_rejects_out_of_range_lane_without_padding_configs(
        monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", video_lane_count=1)
    scene.video_lane_configs = [LaneConfig(name="Only")]
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="proj", scenes=[scene],
        assets=[Asset(
            asset_id="video", asset_type="video", path="media/video.mp4",
            frame_count=24, duration_sec=1.0)])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "create_clip", "fields": {
            "asset_id": "video", "track_index": 99,
        }},
    ], saves)

    assert response.status == 404
    assert _response_json(response)["code"] == "item_not_found"
    assert scene.video_lane_count == 1
    assert len(scene.video_lane_configs) == 1
    assert scene.clips == []
    assert saves == []


def test_audio_create_rejects_out_of_range_lane_without_padding_configs(
        monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", audio_lane_count=1)
    scene.audio_lane_configs = [LaneConfig(name="Only")]
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="proj", scenes=[scene],
        assets=[Asset(
            asset_id="audio", asset_type="audio", path="media/audio.wav",
            duration_sec=1.0, duration_checked=True)])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "create_audio_track", "fields": {
            "asset_id": "audio", "lane_index": 99,
        }},
    ], saves)

    assert response.status == 404
    assert _response_json(response)["code"] == "item_not_found"
    assert scene.audio_lane_count == 1
    assert len(scene.audio_lane_configs) == 1
    assert scene.audio_tracks == []
    assert saves == []


@pytest.mark.parametrize(("has_audio", "prepared", "status", "code"), [
    (False, None, 400, "no_embedded_audio"),
    (True, None, 500, "audio_extraction_failed"),
])
def test_create_audio_track_from_video_reports_extraction_refusals(
        monkeypatch, tmp_path, has_audio, prepared, status, code):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene")
    video = Asset(
        asset_id="video", asset_type="video", path="media/video.mp4",
        frame_count=24, duration_sec=1.0, has_audio=has_audio)
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="proj", scenes=[scene],
        assets=[video])
    saves = []
    monkeypatch.setattr(
        route_module, "_prepare_video_audio_asset", lambda *_args: prepared)

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "create_audio_track", "fields": {"asset_id": "video"}},
    ], saves)

    assert response.status == status
    assert _response_json(response)["code"] == code
    assert scene.audio_tracks == []
    assert saves == []


def test_create_driver_validation_refuses_in_operation_order(
        monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene", motion_driver_lane_count=1)
    video = Asset(
        asset_id="video", asset_type="video", path="media/video.mp4",
        frame_count=24, duration_sec=1.0)
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="proj", scenes=[scene],
        assets=[video])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "create_clip", "fields": {
            "asset_id": "video", "role": "motion_driver"}},
        {"type": "create_clip", "fields": {
            "asset_id": "video", "role": "motion_driver"}},
    ], saves)

    assert response.status == 409
    assert _response_json(response)["code"] == "driver_lane_occupied"
    assert len(scene.clips) == 1
    assert saves == []

    scene.video_lane_count = 1
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "create_clip", "fields": {
            "asset_id": "video", "role": "motion_driver"}},
        {"type": "set_lane_count", "lane_type": "video", "count": 2},
    ], saves)
    assert response.status == 409
    assert scene.video_lane_count == 1, "an operation after the invalid create must not run"
    assert len(scene.clips) == 1
    assert saves == []


def test_scene_mutation_batch_caps_media_io_creates(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene")
    video = Asset(
        asset_id="video", asset_type="video", path="media/video.mp4",
        frame_count=24, duration_sec=1.0, has_audio=True)
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="proj", scenes=[scene],
        assets=[video])
    saves = []
    monkeypatch.setattr(
        route_module, "_prepare_video_audio_asset",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("batch cap must run before extraction")))

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "create_clip", "fields": {
            "asset_id": "video", "dual_drop": True}},
        {"type": "create_clip", "fields": {
            "asset_id": "video", "dual_drop": True}},
    ], saves)

    assert response.status == 400
    assert _response_json(response)["code"] == "too_many_media_io_operations"
    assert scene.clips == []
    assert saves == []


def test_media_io_cap_ignores_non_extracting_media_creates(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(
        scene_id="scene", video_lane_count=1, audio_lane_count=1,
        motion_driver_lane_count=1)
    video = Asset(
        asset_id="video", asset_type="video", path="media/video.mp4",
        frame_count=24, duration_sec=1.0, has_audio=True)
    silent_video = Asset(
        asset_id="silent", asset_type="video", path="media/silent.mp4",
        frame_count=24, duration_sec=1.0, has_audio=False)
    derived_audio = Asset(
        asset_id="derived", asset_type="audio", path="media/video_audio.wav",
        duration_sec=1.0, duration_checked=True)
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="proj", scenes=[scene],
        assets=[video, silent_video])
    saves = []
    extraction_calls = []

    def prepare(*_args):
        extraction_calls.append(True)
        return derived_audio

    monkeypatch.setattr(route_module, "_prepare_video_audio_asset", prepare)
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "create_clip", "fields": {
            "asset_id": "video", "dual_drop": True,
        }},
        {"type": "create_clip", "fields": {
            "asset_id": "video", "role": "motion_driver", "dual_drop": True,
        }},
    ], saves)

    assert response.status == 200
    assert len(extraction_calls) == 1
    assert len(scene.clips) == 2
    assert len(scene.audio_tracks) == 1
    assert len(saves) == 1

    refused = _apply_scene_operations(route_module, monkeypatch, project, "scene", [
        {"type": "create_audio_track", "fields": {"asset_id": "silent"}},
        {"type": "create_audio_track", "fields": {"asset_id": "silent"}},
    ], saves)
    assert refused.status == 400
    assert _response_json(refused)["code"] == "no_embedded_audio"
    assert len(extraction_calls) == 1


def test_sonder_route_path_strips_single_api_segment(monkeypatch):
    route_module = _load_route_module(monkeypatch)

    assert route_module._sonder_route_path("/api/sonder-editor/project/p") == "/sonder-editor/project/p"
    assert route_module._sonder_route_path("/sonder-editor/project/p") == "/sonder-editor/project/p"
    assert route_module._sonder_route_path("/apifoo/sonder-editor/x") == "/apifoo/sonder-editor/x"
    assert route_module._sonder_route_path("/api") == "/api"
    # Single strip only: a double /api prefix stays unmatched by design.
    assert route_module._sonder_route_path("/api/api/sonder-editor/x") == "/api/sonder-editor/x"


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_project_version_header_middleware_attaches_loaded_project_version(monkeypatch, prefix):
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
        path=f"{prefix}/sonder-editor/project/project-1/scenes/scene-1",
    )
    response = asyncio.run(route_module._project_version_header_middleware(request, handler))

    assert response.headers["X-Sonder-Project-Id"] == "project-1"
    assert response.headers["X-Sonder-Project-Modified-At"] == "version-1"


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_sonder_security_middleware_adds_security_headers(monkeypatch, prefix):
    route_module = _load_route_module(monkeypatch)

    async def handler(_request):
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="GET",
        path=f"{prefix}/sonder-editor/project/project-1/assets",
        headers={"Host": "127.0.0.1:7822"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))

    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert response.headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_sonder_security_middleware_blocks_cross_origin_mutation(monkeypatch, prefix):
    route_module = _load_route_module(monkeypatch)
    called = []

    async def handler(_request):
        called.append(True)
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="POST",
        path=f"{prefix}/sonder-editor/project/project-1/assets/sync",
        headers={"Host": "127.0.0.1:7822", "Origin": "https://example.invalid"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))
    payload = _response_json(response)

    assert response.status == 403
    assert payload["code"] == "cross_origin_blocked"
    assert called == []
    assert response.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_sonder_security_middleware_allows_same_origin_mutation(monkeypatch, prefix):
    route_module = _load_route_module(monkeypatch)
    called = []

    async def handler(_request):
        called.append(True)
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="POST",
        path=f"{prefix}/sonder-editor/project/project-1/assets/sync",
        headers={"Host": "127.0.0.1:7822", "Origin": "http://127.0.0.1:7822"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))

    assert response.status == 200
    assert called == [True]


@pytest.mark.parametrize("prefix", ["", "/api"])
def test_sonder_security_middleware_allows_absent_origin_mutation(monkeypatch, prefix):
    route_module = _load_route_module(monkeypatch)
    called = []

    async def handler(_request):
        called.append(True)
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="POST",
        path=f"{prefix}/sonder-editor/project/project-1/assets/sync",
        headers={"Host": "127.0.0.1:7822"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))

    assert response.status == 200
    assert called == [True]


def test_sonder_security_middleware_ignores_non_sonder_api_paths(monkeypatch):
    route_module = _load_route_module(monkeypatch)

    async def handler(_request):
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="GET",
        path="/api/prompt",
        headers={"Host": "127.0.0.1:7822"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))

    assert response.status == 200
    assert "Content-Security-Policy" not in response.headers


def test_scene_mutation_remove_lane_is_single_save_and_reindexes(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig(name="V0"), LaneConfig(name="V1"), LaneConfig(name="V2")]
    scene.clips = [
        ClipReference(clip_id="clip-0", timeline_start_frame=0, timeline_end_frame=10, track_index=0),
        ClipReference(clip_id="clip-1", timeline_start_frame=10, timeline_end_frame=20, track_index=1),
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
                "expected": {"lane_count": 3, "config": {
                    "name": "V1", "color": "", "locked": False, "hidden": False}},
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
    displaced = scene.guide_frames[1].guide_id
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
                # Naming the guide at the destination is what makes this the
                # DELIBERATE replacement rather than a blind one.
                "expected": {"frame_index": 5, "asset_id": "asset-a",
                             "replaces_guide_id": displaced},
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
            # Matching, so the identity guard passes and the Driver refusal is
            # still what this test observes. `role` is in the guarded set
            # precisely so a concurrent conversion refuses at the dispatch
            # instead of 409-ing deeper in; here it agrees, so it does not.
            "expected": {
                "clip_id": "driver-1",
                "timeline_start_frame": 0,
                "timeline_end_frame": 20,
                "track_index": 0,
                "role": "motion_driver",
            },
        }]},
    )))

    assert response.status == 409
    assert _response_json(response)["code"] == "driver_clip_split_refused"
    assert saves == []
    assert len(scene.clips) == 1
    assert len(scene.audio_tracks) == 1
    assert driver.timeline_end_frame == 20
    assert audio.timeline_end_frame == 20


# ---------------------------------------------------------------------------
# Media-split anchor guards (umbrella Phase C stage 2 L1)
# ---------------------------------------------------------------------------
# The tracked Critical defect is a cut aimed at a clip whose bounds the server
# had already changed: it fell through `_apply_split_linked`'s `else` arm,
# returned 200 with `split_count: 0`, changed nothing and still paid a full
# document write. Two guards close it, and they answer different questions --
# `expected` says "this is not the row you saw" (409) and the anchor bounds
# check says "this frame was never inside it" (400). Neither may write.

def _split_scene_project(monkeypatch, route_module, tmp_path, *, linked=False):
    """One clip [0,20] on lane 0, one audio [0,20], optionally linked."""
    clip = ClipReference(
        clip_id="clip-1",
        source_path="media/a.mp4",
        timeline_start_frame=0,
        timeline_end_frame=20,
        source_in_frame=0,
        source_out_frame=20,
        total_source_frames=20,
        track_index=0,
    )
    audio = AudioTrack(
        track_id="audio-1",
        source_path="media/a.wav",
        timeline_start_frame=0,
        timeline_end_frame=20,
        source_in_frame=0,
        total_source_frames=20,
        lane_index=0,
    )
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=40)
    scene.clips = [clip]
    scene.audio_tracks = [audio]
    if linked:
        scene.linked_item_groups = [{
            "group_id": "group-1",
            "items": [{"type": "clip", "id": "clip-1"},
                      {"type": "audio", "id": "audio-1"}],
        }]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda saved_project: saves.append(saved_project))
    return scene, clip, audio, saves


def _split_clip_op(frame, expected=None, **overrides):
    guard = {
        "clip_id": "clip-1",
        "timeline_start_frame": 0,
        "timeline_end_frame": 20,
        "track_index": 0,
        "role": "render",
    }
    guard.update(expected or {})
    operation = {"type": "split_clip", "clip_id": "clip-1", "frame": frame,
                 "apply_linked": False, "expected": guard}
    operation.update(overrides)
    return operation


def _split_audio_op(frame, expected=None, **overrides):
    guard = {
        "track_id": "audio-1",
        "timeline_start_frame": 0,
        "timeline_end_frame": 20,
        "lane_index": 0,
    }
    guard.update(expected or {})
    operation = {"type": "split_audio_track", "track_id": "audio-1", "frame": frame,
                 "apply_linked": False, "expected": guard}
    operation.update(overrides)
    return operation


def _run_operations(route_module, operations):
    return asyncio.run(_mutation_handler(route_module)(DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene-1"},
        body={"operations": operations},
    )))


def test_split_clip_with_matching_expected_splits_normally(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)

    response = _run_operations(route_module, [_split_clip_op(10)])

    assert response.status == 200
    assert len(scene.clips) == 2
    assert clip.timeline_end_frame == 10
    right = next(item for item in scene.clips if item.clip_id != "clip-1")
    assert (right.timeline_start_frame, right.timeline_end_frame) == (10, 20)
    assert len(saves) == 1


def test_split_clip_without_expected_is_refused_before_any_write(monkeypatch, tmp_path):
    """The legacy REST route may omit `expected`; a dispatcher operation may not."""
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)

    operation = _split_clip_op(10)
    del operation["expected"]
    response = _run_operations(route_module, [operation])

    assert response.status == 400
    assert _response_json(response)["code"] == "missing_expected_identity"
    assert saves == []
    assert len(scene.clips) == 1
    assert clip.timeline_end_frame == 20


def test_split_clip_refuses_a_partial_expected_rather_than_half_guarding(
        monkeypatch, tmp_path):
    """`_require_expected` is a key-SET check, so a subset never half-guards."""
    route_module = _load_route_module(monkeypatch)
    scene, _clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)

    operation = _split_clip_op(10)
    operation["expected"] = {"clip_id": "clip-1"}
    response = _run_operations(route_module, [operation])

    assert response.status == 400
    assert _response_json(response)["code"] == "missing_expected_identity"
    assert saves == []
    assert len(scene.clips) == 1


def test_split_clip_whose_start_moved_refuses_without_writing(monkeypatch, tmp_path):
    """The staleness the guard exists for, and the one the bounds check cannot
    see: a clip that MOVED still contains the frame, but the cut would land at
    a different source offset than the author pointed at."""
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)
    clip.timeline_start_frame = 5
    clip.timeline_end_frame = 25

    response = _run_operations(route_module, [_split_clip_op(10)])

    assert response.status == 409
    assert _response_json(response)["code"] == "identity_mismatch"
    assert "start frame" in _response_json(response)["error"], (
        "a refusal must name what moved: " + _response_json(response)["error"])
    assert saves == [], "a refused split must not pay a document write"
    assert len(scene.clips) == 1
    assert clip.timeline_start_frame == 5


def test_a_cut_aimed_left_of_an_earlier_one_still_lands(monkeypatch, tmp_path):
    """`timeline_end_frame` is deliberately NOT guarded, and this is why.

    The end is the field the author's own previous cut rewrites. A clip [0,20]
    cut at 12 leaves [0,12]; a second cut queued against the pre-cut view and
    aimed at frame 8 is still inside that left half and split correctly before
    the guard existed. Guarding the end would 409 it -- roughly half of every
    rapid-cut burst, turned from working into refused. Probed on the live route
    before this test was written.
    """
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)
    clip.timeline_end_frame = 12      # an earlier cut in the same burst landed
    clip.source_out_frame = 12

    response = _run_operations(route_module, [_split_clip_op(8)])

    assert response.status == 200, _response_json(response)
    assert sorted((c.timeline_start_frame, c.timeline_end_frame)
                  for c in scene.clips) == [(0, 8), (8, 12)]
    assert len(saves) == 1


def test_a_cut_aimed_past_a_shortened_end_refuses_loudly(monkeypatch, tmp_path):
    """The complementary direction, and the tracked defect's exact shape.

    Same burst, but the second cut is aimed RIGHT of the first, so the clip it
    names no longer contains the frame. Before this landing that returned 200
    with `split_count: 0` and still paid a full write. The anchor bounds check
    is what catches it -- not the identity guard, which passes here.
    """
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)
    clip.timeline_end_frame = 8
    clip.source_out_frame = 8

    response = _run_operations(route_module, [_split_clip_op(14)])

    assert response.status == 400
    assert _response_json(response)["code"] == "invalid_range"
    assert saves == []
    assert len(scene.clips) == 1
    assert clip.timeline_end_frame == 8


def test_split_clip_whose_lane_moved_refuses(monkeypatch, tmp_path):
    """`track_index` is guarded because the lane is what the lock check read."""
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)
    clip.track_index = 2

    response = _run_operations(route_module, [_split_clip_op(10)])

    assert response.status == 409
    assert _response_json(response)["code"] == "identity_mismatch"
    assert saves == []
    assert len(scene.clips) == 1


def test_split_clip_whose_role_became_driver_refuses_at_the_guard(monkeypatch, tmp_path):
    """`role` is in the set so a concurrent conversion refuses at the dispatch
    rather than 409-ing deeper in with a message about a clip the author never
    aimed at."""
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)
    clip.role = "motion_driver"

    response = _run_operations(route_module, [_split_clip_op(10)])

    assert response.status == 409
    assert _response_json(response)["code"] == "identity_mismatch"
    assert saves == []
    assert len(scene.clips) == 1


def test_split_clip_ignores_a_concurrent_source_trim(monkeypatch, tmp_path):
    """The other half of the guard's scope. The set is deliberately narrow, so
    an unrelated property edit must NOT refuse a cut the author could still
    make -- over-guarding is its own defect."""
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)
    clip.total_source_frames = 999
    clip.opacity = 0.5

    response = _run_operations(route_module, [_split_clip_op(10)])

    assert response.status == 200
    assert len(scene.clips) == 2
    assert len(saves) == 1


def test_split_clip_outside_its_own_bounds_refuses_instead_of_no_op(monkeypatch, tmp_path):
    """Guard 1, reached when `expected` agrees and the frame is simply wrong.

    This is the arm that used to return 200, `split_count: 0` and a full write.
    """
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)

    response = _run_operations(route_module, [_split_clip_op(30)])

    assert response.status == 400
    assert _response_json(response)["code"] == "invalid_range"
    assert saves == []
    assert len(scene.clips) == 1
    assert clip.timeline_end_frame == 20


def test_split_clip_on_a_boundary_frame_refuses(monkeypatch, tmp_path):
    """`start < f < end` is strict at both ends, as `_split_clip_object` is."""
    route_module = _load_route_module(monkeypatch)
    scene, _clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)

    for frame in (0, 20):
        response = _run_operations(route_module, [_split_clip_op(frame)])
        assert response.status == 400, frame
        assert _response_json(response)["code"] == "invalid_range", frame
    assert saves == []
    assert len(scene.clips) == 1


def test_linked_split_still_divides_every_member(monkeypatch, tmp_path):
    """The anchor guard is anchor-SCOPED: partners keep their own behaviour."""
    route_module = _load_route_module(monkeypatch)
    scene, clip, audio, saves = _split_scene_project(
        monkeypatch, route_module, tmp_path, linked=True)

    response = _run_operations(route_module, [_split_clip_op(10, apply_linked=True)])

    assert response.status == 200
    assert clip.timeline_end_frame == 10
    assert audio.timeline_end_frame == 10
    assert len(scene.clips) == 2
    assert len(scene.audio_tracks) == 2
    assert len(saves) == 1


def test_a_linked_partner_lying_wholly_past_the_cut_is_not_an_error(monkeypatch, tmp_path):
    """`split_count == len(refs)` is never a valid assertion, so the anchor
    guard must not quietly become one: a partner outside the cut is filed into
    the right-hand group, not refused."""
    route_module = _load_route_module(monkeypatch)
    scene, clip, audio, saves = _split_scene_project(
        monkeypatch, route_module, tmp_path, linked=True)
    audio.timeline_start_frame = 14
    audio.timeline_end_frame = 20

    response = _run_operations(route_module, [_split_clip_op(10, apply_linked=True)])

    assert response.status == 200
    results = _response_json(response)["results"]
    assert results[0]["split_count"] == 1
    assert len(scene.audio_tracks) == 1, "the partner must not have been split"
    assert audio.timeline_start_frame == 14
    assert len(saves) == 1


def test_split_audio_track_carries_the_same_guard(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene, _clip, audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)

    response = _run_operations(route_module, [
        _split_audio_op(10, expected={"timeline_start_frame": 3})])
    assert response.status == 409
    assert _response_json(response)["code"] == "identity_mismatch"
    assert "audio track" in _response_json(response)["error"]
    assert saves == []
    assert len(scene.audio_tracks) == 1

    response = _run_operations(route_module, [_split_audio_op(10)])
    assert response.status == 200
    assert audio.timeline_end_frame == 10
    assert len(scene.audio_tracks) == 2
    assert len(saves) == 1


def test_split_audio_track_without_expected_is_refused(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene, _clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)

    operation = _split_audio_op(10)
    del operation["expected"]
    response = _run_operations(route_module, [operation])

    assert response.status == 400
    assert _response_json(response)["code"] == "missing_expected_identity"
    assert saves == []
    assert len(scene.audio_tracks) == 1


def test_a_refused_split_aborts_the_whole_batch(monkeypatch, tmp_path):
    """One request, one save, so a refusal anywhere must leave nothing DURABLE.

    Stated precisely, because the in-memory claim would be false: the earlier
    `update_clip` does reach the model -- `_apply_scene_mutations_sync` applies
    operations in order and the refusal unwinds by raising, not by rolling back.
    What makes that harmless is that the write never happens and the next
    request reloads from disk, so the assertion that carries the weight is
    `saves == []`, not the model's state. Asserted both ways round so a future
    change to either half is visible.
    """
    route_module = _load_route_module(monkeypatch)
    scene, clip, _audio, saves = _split_scene_project(monkeypatch, route_module, tmp_path)

    response = _run_operations(route_module, [
        {"type": "update_clip", "clip_id": "clip-1", "fields": {"opacity": 0.25}},
        _split_clip_op(10, expected={"timeline_start_frame": 999}),
    ])

    assert response.status == 409
    assert saves == [], "nothing durable may survive a refused batch"
    assert len(scene.clips) == 1
    assert clip.opacity == 0.25, (
        "the in-memory model IS mutated by the operations preceding the "
        "refusal; only the absent write makes that harmless")


def test_the_prompt_split_inherits_the_anchor_bounds_check(monkeypatch, tmp_path):
    """The third anchor type, and the one nobody set out to change.

    The bounds check lives in `_apply_split_linked`, which `split_prompt_section`
    also calls, so the prompt split gained it too. That is reachable rather than
    theoretical: `_rebaseSceneMutationIntentForHistory` DOES rebase this
    operation -- it retargets `index` and re-snapshots `expected` onto the
    ordered row, so the identity guard passes -- but it never rebases `frame`.
    A prompt split queued behind an Undo that retimed its section therefore
    arrives with a frame outside the section it names, and now refuses loudly
    where it used to return 200 with `split_count: 0` and pay a full write.
    """
    route_module = _load_route_module(monkeypatch)
    section = PromptSection(0, 10, channels={"visual": "text"})
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=40)
    scene.prompt_sections = [section]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda saved_project: saves.append(saved_project))

    def split(frame):
        return asyncio.run(_mutation_handler(route_module)(DummyRequest(
            match_info={"project_id": "proj", "scene_id": "scene-1"},
            body={"operations": [{
                "type": "split_prompt_section", "index": 0, "frame": frame,
                "apply_linked": False,
                "expected": {"prompt_id": section.prompt_id,
                             "start_frame": 0, "end_frame": 10},
            }]})))

    response = split(25)
    assert response.status == 400
    assert _response_json(response)["code"] == "invalid_range"
    assert saves == []
    assert len(scene.prompt_sections) == 1

    response = split(5)
    assert response.status == 200
    assert len(scene.prompt_sections) == 2
    assert len(saves) == 1


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
            "expected": {"start_frame": 20, "end_frame": 30,
                         "channels": copy.deepcopy(scene.prompt_sections[1].channels)},
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
    # and the default template-owned label policy, NOT the client display value
    assert job.prompt == "global [VISUAL]: first, second"
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
    assert prompts == [
        "g [VISUAL]: chunk one action",
        "g [VISUAL]: chunk two action",
    ]


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


def test_asset_sync_stale_version_returns_409_conflict(monkeypatch, tmp_path):
    # After a generation commit bumps the project version, the editor's asset
    # refresh POSTs /assets/sync with a now-stale If-Match. The load-time
    # precondition raises ProjectVersionConflict, which the shared conflict
    # middleware renders as the 409 the client reconcile handler parses.
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = TimelineProject(project_dir=str(project_dir), name="Project")
    project.project_id = "proj"
    route_module.save_project(project)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    base_version = project.modified_at

    # A later writer (mirrors a Save Video/Bridge generation commit) bumps the
    # on-disk version past what the client still holds in its If-Match.
    current = route_module.load_project(str(project_dir))
    route_module.save_project(current)
    assert current.modified_at != base_version

    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/sync")
    request = DummyRequest(
        match_info={"project_id": "proj"},
        headers={"If-Match": base_version},
        method="POST",
        path="/sonder-editor/project/proj/assets/sync",
    )
    response = asyncio.run(route_module._project_error_middleware(request, handler))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["code"] == "project_version_conflict"
    assert payload["expected_modified_at"] == base_version
    assert payload["actual_modified_at"] == current.modified_at
    assert payload["project"]["project_id"] == "proj"


def test_save_path_version_mismatch_returns_projection_and_healing_headers(
        monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    stale = TimelineProject(project_dir=str(project_dir), name="Project")
    stale.project_id = "proj"
    stale.prompt_semantic_units = [{
        "semantic_unit_id": "unit", "name": "Hero", "future": {"kept": True}}]
    route_module.save_project(stale)
    expected = stale.modified_at

    current = route_module.load_project(str(project_dir))
    current.prompt_context_profiles = [{
        "profile_id": "profile", "name": "Profile", "future": [1, 2]}]
    route_module.save_project(current)
    assert current.modified_at != expected

    async def stale_save(_request):
        route_module.save_project(stale, expected_modified_at=expected)
        return web.json_response({"unexpected": True})

    request = DummyRequest(
        match_info={"project_id": "proj"},
        method="POST", path="/sonder-editor/project/proj/test-save")
    response = asyncio.run(
        route_module._project_error_middleware(request, stale_save))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["code"] == "project_version_conflict"
    assert payload["expected_modified_at"] == expected
    assert payload["actual_modified_at"] == current.modified_at
    assert set(payload["project"]) == {
        "project_id", "modified_at",
        "prompt_semantic_units", "prompt_context_profiles",
    }
    assert payload["project"]["prompt_semantic_units"][0]["future"] == {
        "kept": True}
    assert payload["project"]["prompt_context_profiles"][0]["future"] == [1, 2]
    assert response.headers["X-Sonder-Project-Id"] == "proj"
    assert response.headers["X-Sonder-Project-Modified-At"] == current.modified_at


def test_scene_mutation_consolidates_video_and_removes_only_vacated_lanes(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 5
    scene.video_lane_configs = [LaneConfig(name=f"V{index}") for index in range(5)]
    scene.audio_tracks = [AudioTrack(track_id="linked-audio", timeline_start_frame=0, timeline_end_frame=10)]
    scene.clips = [
        ClipReference(clip_id="clip-a", timeline_start_frame=0, timeline_end_frame=10, track_index=1),
        ClipReference(clip_id="clip-b", timeline_start_frame=10, timeline_end_frame=20, track_index=3),
        ClipReference(clip_id="target-adjacent", timeline_start_frame=20, timeline_end_frame=30, track_index=3),
        ClipReference(clip_id="source-survivor", timeline_start_frame=40, timeline_end_frame=50, track_index=4),
    ]
    scene.linked_item_groups = [{
        "group_id": "take-a",
        "items": [{"type": "clip", "id": "clip-a"}, {"type": "audio", "id": "linked-audio"}],
    }]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "consolidate_items",
        "lane_type": "video",
        "item_ids": ["clip-a", "clip-b"],
        "target_lane": 3,
        "remove_vacated_lanes": True,
    }], saves)

    assert response.status == 200
    assert len(saves) == 1
    assert scene.video_lane_count == 4
    assert [config.name for config in scene.video_lane_configs] == ["V0", "V2", "V3", "V4"]
    assert {clip.clip_id: clip.track_index for clip in scene.clips} == {
        "clip-a": 2,
        "clip-b": 2,
        "target-adjacent": 2,
        "source-survivor": 3,
    }
    assert scene.linked_item_groups == [{
        "group_id": "take-a",
        "items": [{"type": "clip", "id": "clip-a"}, {"type": "audio", "id": "linked-audio"}],
    }]
    result = _response_json(response)["results"][0]
    assert result == {
        "type": "consolidate_items",
        "lane_type": "video",
        "moved_count": 2,
        "removed_lanes": [1],
        "target_lane": 2,
    }


def test_scene_mutation_consolidates_audio_and_keeps_nonvacated_source(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.audio_lane_count = 3
    scene.audio_lane_configs = [LaneConfig(name=f"A{index}") for index in range(3)]
    scene.audio_tracks = [
        AudioTrack(track_id="audio-a", timeline_start_frame=0, timeline_end_frame=10, lane_index=0),
        AudioTrack(track_id="audio-b", timeline_start_frame=10, timeline_end_frame=20, lane_index=2),
        AudioTrack(track_id="source-survivor", timeline_start_frame=30, timeline_end_frame=40, lane_index=0),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "consolidate_items",
        "lane_type": "audio",
        "item_ids": ["audio-a", "audio-b"],
        "target_lane": 2,
        "remove_vacated_lanes": True,
    }], saves)

    assert response.status == 200
    assert len(saves) == 1
    assert scene.audio_lane_count == 3
    assert {track.track_id: track.lane_index for track in scene.audio_tracks} == {
        "audio-a": 2,
        "audio-b": 2,
        "source-survivor": 0,
    }
    assert _response_json(response)["results"][0]["removed_lanes"] == []


def test_scene_mutation_consolidation_collision_rejects_without_save(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig() for _ in range(3)]
    scene.clips = [
        ClipReference(clip_id="clip-a", timeline_start_frame=0, timeline_end_frame=12, track_index=0),
        ClipReference(clip_id="clip-b", timeline_start_frame=10, timeline_end_frame=20, track_index=1),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "consolidate_items",
        "lane_type": "video",
        "item_ids": ["clip-a", "clip-b"],
        "target_lane": 1,
        "remove_vacated_lanes": True,
    }], saves)

    assert response.status == 409
    assert _response_json(response)["code"] == "lane_collision"
    assert saves == []
    assert {clip.clip_id: clip.track_index for clip in scene.clips} == {"clip-a": 0, "clip-b": 1}


def test_scene_mutation_consolidation_rejects_destination_item_collision(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig() for _ in range(3)]
    scene.clips = [
        ClipReference(clip_id="clip-a", timeline_start_frame=0, timeline_end_frame=10, track_index=0),
        ClipReference(clip_id="clip-b", timeline_start_frame=10, timeline_end_frame=20, track_index=1),
        ClipReference(clip_id="target", timeline_start_frame=5, timeline_end_frame=8, track_index=1),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "consolidate_items",
        "lane_type": "video",
        "item_ids": ["clip-a", "clip-b"],
        "target_lane": 1,
        "remove_vacated_lanes": True,
    }], saves)

    assert response.status == 409
    assert _response_json(response)["code"] == "lane_collision"
    assert saves == []


def test_scene_mutation_consolidation_validates_selection_and_locks(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig(), LaneConfig(locked=True), LaneConfig()]
    scene.clips = [
        ClipReference(clip_id="clip-a", timeline_start_frame=0, timeline_end_frame=10, track_index=0),
        ClipReference(clip_id="driver", timeline_start_frame=10, timeline_end_frame=20, track_index=0, role="motion_driver"),
        ClipReference(clip_id="clip-b", timeline_start_frame=10, timeline_end_frame=20, track_index=1),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])

    for item_ids, expected_status, expected_code in [
        (["clip-a", "clip-a"], 400, "invalid_consolidation"),
        (["clip-a", "missing"], 404, "item_not_found"),
        (["clip-a", "driver"], 400, "invalid_consolidation"),
        (["clip-a", "clip-b"], 409, "track_locked"),
    ]:
        saves = []
        response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
            "type": "consolidate_items",
            "lane_type": "video",
            "item_ids": item_ids,
            "target_lane": 1,
            "remove_vacated_lanes": True,
        }], saves)
        assert response.status == expected_status
        assert _response_json(response)["code"] == expected_code
        assert saves == []


def test_scene_mutation_remove_lane_move_collision_never_deletes(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.motion_driver_lane_count = 2
    scene.motion_driver_lane_configs = [LaneConfig(), LaneConfig()]
    scene.clips = [
        ClipReference(clip_id="driver-a", timeline_start_frame=0, timeline_end_frame=10, track_index=0, role="motion_driver"),
        ClipReference(clip_id="driver-b", timeline_start_frame=0, timeline_end_frame=10, track_index=1, role="motion_driver"),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "remove_lane",
        "lane_type": "motion_driver",
        "lane_index": 1,
        "item_policy": "move_items",
        "target_lane": 0,
        "expected": {"lane_count": 2, "config": {"name": "", "color": "", "locked": False, "hidden": False}},
    }], saves)

    assert response.status == 409
    assert _response_json(response)["code"] == "lane_collision"
    assert saves == []
    assert scene.motion_driver_lane_count == 2
    assert {clip.clip_id for clip in scene.clips} == {"driver-a", "driver-b"}


def test_scene_mutation_remove_lane_refuses_video_and_audio_collisions(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    cases = []

    video_scene = Scene(scene_id="video-scene", name="Video")
    video_scene.video_lane_count = 2
    video_scene.video_lane_configs = [LaneConfig(), LaneConfig()]
    video_scene.clips = [
        ClipReference(clip_id="video-a", timeline_start_frame=0, timeline_end_frame=10, track_index=0),
        ClipReference(clip_id="video-b", timeline_start_frame=5, timeline_end_frame=15, track_index=1),
    ]
    cases.append((video_scene, "video"))

    audio_scene = Scene(scene_id="audio-scene", name="Audio")
    audio_scene.audio_lane_count = 2
    audio_scene.audio_lane_configs = [LaneConfig(), LaneConfig()]
    audio_scene.audio_tracks = [
        AudioTrack(track_id="audio-a", timeline_start_frame=0, timeline_end_frame=10, lane_index=0),
        AudioTrack(track_id="audio-b", timeline_start_frame=5, timeline_end_frame=15, lane_index=1),
    ]
    cases.append((audio_scene, "audio"))

    for scene, lane_type in cases:
        project = TimelineProject(project_dir=str(tmp_path / lane_type), name="Project", scenes=[scene])
        saves = []
        response = _apply_scene_operations(route_module, monkeypatch, project, scene.scene_id, [{
            "type": "remove_lane",
            "lane_type": lane_type,
            "lane_index": 1,
            "item_policy": "move_items",
            "target_lane": 0,
            "expected": {"lane_count": 2, "config": {"name": "", "color": "", "locked": False, "hidden": False}},
        }], saves)
        assert response.status == 409
        assert _response_json(response)["code"] == "lane_collision"
        assert saves == []


def test_scene_mutation_trim_collision_guard_rejects_overlap_and_allows_adjacent(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_configs = [LaneConfig()]
    scene.clips = [
        ClipReference(clip_id="clip-a", timeline_start_frame=0, timeline_end_frame=10, source_out_frame=10, total_source_frames=30, track_index=0),
        ClipReference(clip_id="clip-b", timeline_start_frame=15, timeline_end_frame=25, source_out_frame=10, total_source_frames=10, track_index=0),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    rejected = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "update_clip",
        "clip_id": "clip-a",
        "fields": {"timeline_start_frame": 0, "timeline_end_frame": 20, "source_out_frame": 20},
        "validate_lane_collision": True,
    }], saves)
    assert rejected.status == 409
    assert _response_json(rejected)["code"] == "lane_collision"
    assert saves == []
    assert scene.clips[0].timeline_end_frame == 10

    accepted = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "update_clip",
        "clip_id": "clip-a",
        "fields": {"timeline_start_frame": 0, "timeline_end_frame": 15, "source_out_frame": 15},
        "validate_lane_collision": True,
    }], saves)
    assert accepted.status == 200
    assert len(saves) == 1
    assert scene.clips[0].timeline_end_frame == 15


def test_scene_mutation_linked_trim_collision_refuses_atomically(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_configs = [LaneConfig()]
    scene.audio_lane_configs = [LaneConfig()]
    scene.clips = [
        ClipReference(clip_id="clip-a", timeline_start_frame=0, timeline_end_frame=10, source_out_frame=10, total_source_frames=30, track_index=0),
    ]
    scene.audio_tracks = [
        AudioTrack(track_id="audio-a", timeline_start_frame=0, timeline_end_frame=10, source_in_frame=0, total_source_frames=30, lane_index=0),
        AudioTrack(track_id="audio-neighbor", timeline_start_frame=12, timeline_end_frame=20, source_in_frame=0, total_source_frames=8, lane_index=0),
    ]
    scene.linked_item_groups = [{
        "group_id": "take-a",
        "items": [{"type": "clip", "id": "clip-a"}, {"type": "audio", "id": "audio-a"}],
    }]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "update_clip",
        "clip_id": "clip-a",
        "fields": {"timeline_start_frame": 0, "timeline_end_frame": 15, "source_out_frame": 15},
        "apply_linked": True,
        "validate_lane_collision": True,
    }], saves)

    assert response.status == 409
    assert _response_json(response)["code"] == "lane_collision"
    assert saves == []
    assert scene.clips[0].timeline_end_frame == 10
    assert scene.audio_tracks[0].timeline_end_frame == 10


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


def test_load_project_repair_save_is_version_guarded_and_non_fatal(monkeypatch, tmp_path):
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
    # and the save is a version-guarded, no-bump/no-broadcast back-fill.
    assert loaded is project
    assert scene.clips[0].total_source_frames == 10
    assert save_calls == [{
        "expected_modified_at": project.modified_at,
        "bump_modified_at": False,
        "notify": False,
    }]


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


def test_prompt_template_dependencies_import_atomically_with_scene_mutation(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project.project_id = "proj"
    saves = []
    profile = {
        "profile_id": "custom:test", "version": "1", "name": "Test",
        "template_id": "standard", "capabilities": {}, "writing_aids": [],
    }
    unit = {
        "semantic_unit_id": "unit-1", "name": "Granny", "order": 0,
        "sources": [], "definition": "A grandmother",
    }

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [
        {"type": "import_prompt_context_dependencies",
         "profiles": [profile], "semantic_units": [unit]},
        {"type": "update_scene_fields",
         "fields": {"prompt_context_profile_id": "custom:test@1"}},
    ], saves)

    assert response.status == 200
    assert len(saves) == 1
    payload = _response_json(response)
    assert payload["prompt_context_profiles"][0]["profile_id"] == "custom:test"
    assert payload["prompt_semantic_units"][0]["semantic_unit_id"] == "unit-1"
    assert scene.prompt_context_profile_id == "custom:test@1"


def test_prompt_template_dependency_collision_blocks_instead_of_overwriting(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project.project_id = "proj"
    project.prompt_semantic_units = [{
        "semantic_unit_id": "unit-1", "name": "Existing", "order": 0,
        "sources": [], "visual_intent": "preserve",
        "audio_intent": "reference_characteristics", "definition": "",
    }]
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "import_prompt_context_dependencies", "profiles": [],
        "semantic_units": [{"semantic_unit_id": "unit-1", "name": "Different"}],
    }], saves)

    assert response.status == 409
    assert saves == []
    assert project.prompt_semantic_units[0]["name"] == "Existing"


@pytest.mark.parametrize(("profiles", "semantic_units"), [
    ([], ["not-an-object"]),
    ([], [{"name": "Missing stable id", "handle": "MissingId"}]),
    (["not-an-object"], []),
    ([{"capabilities": {}, "name": "Missing exact profile key"}], []),
])
def test_prompt_template_dependency_import_rejects_malformed_entries_atomically(
        monkeypatch, profiles, semantic_units):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project.project_id = "proj"
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "import_prompt_context_dependencies", "profiles": profiles,
        "semantic_units": semantic_units,
    }, {
        "type": "update_scene_fields", "fields": {"name": "Must not land"},
    }], saves)

    assert response.status == 400
    assert _response_json(response)["code"] == "invalid_prompt_context_dependencies"
    assert saves == []
    assert project.prompt_semantic_units == []
    assert scene.name == "Scene"


@pytest.mark.parametrize("owner", ["semantic", "physical", "same-import"])
def test_prompt_template_dependency_import_refuses_exact_handle_collisions(
        monkeypatch, owner):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project.project_id = "proj"
    imported = [prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "imported-1", "name": "Imported",
        "handle": "Narrator", "kind": "subject", "sources": [],
    })]
    if owner == "semantic":
        project.prompt_semantic_units = [prompt_context.normalize_semantic_unit({
            "semantic_unit_id": "existing", "name": "Existing",
            "handle": "narrator", "kind": "subject", "sources": [],
        })]
    elif owner == "physical":
        project.references = [ReferenceEntity(
            reference_id="ref", name="Voice", members=[ReferenceMember(
                member_id="member", asset_id="asset", handle="NARRATOR")])]
    else:
        imported.append(prompt_context.normalize_semantic_unit({
            "semantic_unit_id": "imported-2", "name": "Second",
            "handle": "narrator", "kind": "subject", "sources": [],
        }))
    original_units = copy.deepcopy(project.prompt_semantic_units)
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "import_prompt_context_dependencies", "profiles": [],
        "semantic_units": imported,
    }], saves)

    assert response.status == 409
    assert _response_json(response)["code"] == "handle_collision"
    assert saves == []
    assert project.prompt_semantic_units == original_units


def test_cross_project_template_imports_assetless_vocal_identity_with_sections(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project.project_id = "proj"
    saves = []
    unit = route_module.prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "narrator", "handle": "Narrator",
        "name": "Narrator", "kind": "subject", "definition": "Off screen",
        "sources": [], "voice": {"member_id": None},
    })
    event = route_module.prompt_context.normalize_attachment({
        "attachment_id": "vocal", "kind": "vocal_event",
        "source": {"subject_ids": ["narrator"]},
        "config": {"event_type": "dialogue", "text": "Hello"},
    })
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [
        {"type": "import_prompt_context_dependencies", "profiles": [],
         "semantic_units": [unit]},
        {"type": "replace_prompt_sections",
         "expected": {"sections": [s.to_dict() for s in scene.prompt_sections]},
         "sections": [{
            "prompt_id": "p", "start_frame": 0, "end_frame": 24,
            "channels": {"visual": ""}, "attachments": [event],
        }]},
    ], saves)

    assert response.status == 200
    assert len(saves) == 1
    assert project.prompt_semantic_units[0]["semantic_unit_id"] == "narrator"
    assert scene.prompt_sections[0].attachments[0]["source"]["subject_ids"] == [
        "narrator"]


def test_ad_hoc_prompt_identity_is_created_atomically_with_server_handle_and_order(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project.project_id = "proj"
    project.metadata["prompt_channel_template"] = "minimax_h3_ref"
    scene.prompt_context_profile_id = "minimax_h3_ref@1"
    project.prompt_semantic_units = [{
        "semantic_unit_id": "existing", "handle": "Narrator", "name": "Existing",
        "kind": "subject", "order": 7, "sources": [], "definition": "",
        "voice": {"member_id": None}, "attachment_defaults": {},
        "disabled_capabilities": [],
    }]
    saves = []
    attachment = {
        "attachment_id": "vocal-1", "emission_group_id": "vocal-1",
        "kind": "vocal_event", "source": {"subject_ids": ["other-1"]},
        "config": {"event_type": "dialogue", "text": "Hello"},
        "capabilities": [],
    }

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [
        {"type": "create_prompt_semantic_unit", "handle_suggestion": "Narrator",
         "unit": {"semantic_unit_id": "other-1", "name": "Narrator",
                  "kind": "subject", "definition": "Off screen"}},
        {"type": "update_scene_fields", "fields": {"prompt_edit": {
            "documents": {}, "attachments": {"vocal-1": {
                "expected": None, "value": attachment}}}}},
    ], saves)

    assert response.status == 200
    assert len(saves) == 1
    payload = _response_json(response)
    created = payload["results"][0]["unit"]
    assert created["semantic_unit_id"] == "other-1"
    assert created["handle"] == "Narrator2"
    assert created["order"] == 8
    assert created["sources"] == []
    assert "voice" not in created
    assert payload["prompt_semantic_units"][-1] == created
    assert scene.global_attachments[0]["source"]["subject_ids"] == ["other-1"]


@pytest.mark.parametrize(("field", "value"), [
    ("attachment_defaults", {"summary": "edited"}),
    ("disabled_capabilities", ["summary"]),
    ("visual_intent", "transform"),
    ("audio_intent", "replace"),
])
def test_ad_hoc_prompt_identity_replay_refuses_edited_creation_footprint(
        monkeypatch, field, value):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project.project_id = "proj"
    project.metadata["prompt_channel_template"] = "minimax_h3_ref"
    scene.prompt_context_profile_id = "minimax_h3_ref@1"
    unit = prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "other-1", "handle": "Narrator",
        "name": "Narrator", "kind": "subject", "definition": "Off screen",
        "order": 0, "sources": [], "voice": {"member_id": None},
    })
    unit[field] = copy.deepcopy(value)
    project.prompt_semantic_units = [unit]
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "create_prompt_semantic_unit", "handle_suggestion": "Narrator",
        "unit": {"semantic_unit_id": "other-1", "name": "Narrator",
                 "kind": "subject", "definition": "Off screen"},
    }], saves)

    assert response.status == 409
    assert _response_json(response)["code"] == "prompt_semantic_unit_collision"
    assert saves == []
    assert project.prompt_semantic_units == [unit]


def test_multiple_ad_hoc_prompt_identities_and_sections_commit_in_one_mutation(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project.project_id = "proj"
    project.metadata["prompt_channel_template"] = "minimax_h3_ref"
    scene.prompt_context_profile_id = "minimax_h3_ref@1"
    saves = []
    operations = [
        {"type": "create_prompt_semantic_unit", "handle_suggestion": name,
         "unit": {"semantic_unit_id": identity_id, "name": name,
                  "kind": "subject", "definition": ""}}
        for identity_id, name in (("other-1", "One"), ("other-2", "Two"))
    ]
    operations.append({
        "type": "replace_prompt_sections",
        "expected": {"sections": [s.to_dict() for s in scene.prompt_sections]},
        "sections": [{
        "prompt_id": "section-1", "start_frame": 0, "end_frame": 24,
        "channels": {"detailed_description": "A conversation."},
        "attachments": [{
            "attachment_id": "vocal-1", "kind": "vocal_event",
            "source": {"subject_ids": ["other-1", "other-2"]},
            "config": {"event_type": "group_speech", "text": "Together"},
        }],
    }]})

    response = _apply_scene_operations(
        route_module, monkeypatch, project, "scene-1", operations, saves)

    assert response.status == 200
    assert len(saves) == 1
    assert [unit["semantic_unit_id"] for unit in project.prompt_semantic_units] == [
        "other-1", "other-2"]
    assert scene.prompt_sections[0].attachments[0]["source"]["subject_ids"] == [
        "other-1", "other-2"]


def test_ad_hoc_prompt_identity_create_is_idempotent_but_id_collision_refuses(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    project.project_id = "proj"
    project.metadata["prompt_channel_template"] = "minimax_h3_ref"
    scene.prompt_context_profile_id = "minimax_h3_ref@1"
    project.prompt_semantic_units = [route_module.prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "other-1", "handle": "Narrator", "name": "Narrator",
        "kind": "subject", "definition": "Off screen", "sources": [],
    })]

    same = route_module._apply_scene_mutation_operation(project, scene, {
        "type": "create_prompt_semantic_unit", "handle_suggestion": "ignored",
        "unit": {"semantic_unit_id": "other-1", "name": "Narrator",
                 "kind": "subject", "definition": "Off screen"},
    })
    assert same["created"] is False

    with pytest.raises(route_module.ProjectMutationRequestError) as raised:
        route_module._apply_scene_mutation_operation(project, scene, {
            "type": "create_prompt_semantic_unit", "handle_suggestion": "Someone",
            "unit": {"semantic_unit_id": "other-1", "name": "Someone else",
                     "kind": "subject"},
        })
    assert raised.value.code == "prompt_semantic_unit_collision"


def test_ad_hoc_prompt_identity_create_refuses_non_speaking_kind(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])

    with pytest.raises(route_module.ProjectMutationRequestError) as raised:
        route_module._apply_scene_mutation_operation(project, scene, {
            "type": "create_prompt_semantic_unit", "handle_suggestion": "Kitchen",
            "unit": {"semantic_unit_id": "place-1", "name": "Kitchen",
                     "kind": "place", "definition": "A tiled kitchen"},
        })
    assert raised.value.code == "prompt_identity_kind_cannot_speak"
    assert project.prompt_semantic_units == []


def test_guarded_prompt_identity_cleanup_deletes_only_unchanged_unreferenced_unit(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    unit = route_module.prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "other-1", "handle": "Other", "name": "Other",
        "kind": "subject", "definition": "",
    })
    project.prompt_semantic_units = [unit]
    expected = copy.deepcopy(unit)

    scene.global_attachments = [{
        "attachment_id": "vocal-1", "emission_group_id": "vocal-1",
        "kind": "vocal_event", "source": {"subject_ids": ["other-1"]},
        "config": {}, "capabilities": [],
    }]
    referenced = route_module._apply_scene_mutation_operation(project, scene, {
        "type": "delete_prompt_semantic_unit_if_unreferenced",
        "semantic_unit_id": "other-1", "expected": expected,
    })
    assert referenced == {
        "type": "delete_prompt_semantic_unit_if_unreferenced",
        "semantic_unit_id": "other-1", "deleted": False, "reason": "referenced"}

    scene.global_attachments[0]["source"] = {"voice_id": "provider-key"}
    scene.global_attachments[0]["config"] = {
        "audio_speaker_subject_id": "other-1"}
    audio_override = route_module._apply_scene_mutation_operation(project, scene, {
        "type": "delete_prompt_semantic_unit_if_unreferenced",
        "semantic_unit_id": "other-1", "expected": expected,
    })
    assert audio_override["deleted"] is True
    assert project.prompt_semantic_units == []

    scene.global_attachments = []
    project.prompt_semantic_units = [copy.deepcopy(unit)]
    project.prompt_semantic_units[0]["definition"] = "User edited this"
    changed = route_module._apply_scene_mutation_operation(project, scene, {
        "type": "delete_prompt_semantic_unit_if_unreferenced",
        "semantic_unit_id": "other-1", "expected": expected,
    })
    assert changed["reason"] == "changed"
    assert project.prompt_semantic_units

    project.prompt_semantic_units[0]["definition"] = ""
    deleted = route_module._apply_scene_mutation_operation(project, scene, {
        "type": "delete_prompt_semantic_unit_if_unreferenced",
        "semantic_unit_id": "other-1", "expected": expected,
    })
    assert deleted["deleted"] is True
    assert project.prompt_semantic_units == []


def test_guarded_prompt_identity_cleanup_rejects_partial_snapshot(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=24)
    project = TimelineProject(project_dir="", name="Project", scenes=[scene])
    unit = route_module.prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "other-1", "handle": "Other", "name": "Other",
        "kind": "subject", "definition": "",
    })
    project.prompt_semantic_units = [unit]

    result = route_module._apply_scene_mutation_operation(project, scene, {
        "type": "delete_prompt_semantic_unit_if_unreferenced",
        "semantic_unit_id": "other-1",
        "expected": {"semantic_unit_id": "other-1", "name": "Other"},
    })
    assert result["reason"] == "changed"
    assert project.prompt_semantic_units == [unit]

    with pytest.raises(route_module.ProjectMutationRequestError) as raised:
        route_module._apply_scene_mutation_operation(project, scene, {
            "type": "delete_prompt_semantic_unit_if_unreferenced",
            "semantic_unit_id": "other-1",
        })
    assert raised.value.code == "invalid_prompt_semantic_unit_cleanup"


@pytest.mark.parametrize("body_stale", [False, True])
def test_candidate_body_is_the_only_version_gate_and_conflicts_heal(
        monkeypatch, tmp_path, body_stale):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene", duration_frames=20,
                  prompt_sections=[PromptSection(0, 20, channels={"visual": "body"})])
    project = TimelineProject(project_id="proj", project_dir=str(project_dir), scenes=[scene])
    project.prompt_semantic_units = [{
        "semantic_unit_id": "unit-1",
        "name": "Hero",
        "future_unit": {"nested": ["preserved"]},
    }]
    project.prompt_context_profiles = [{
        "profile_id": "profile-1",
        "name": "Custom",
        "future_profile": {"nested": {"preserved": True}},
    }]
    route_module.save_project(project)
    before = copy.deepcopy(project.to_dict())
    expected_projection = {
        "project_id": before["project_id"],
        "modified_at": before["modified_at"],
        "prompt_semantic_units": before["prompt_semantic_units"],
        "prompt_context_profiles": before["prompt_context_profiles"],
    }
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    monkeypatch.setattr(route_module, "load_project", lambda _directory: project)
    saves = []
    monkeypatch.setattr(route_module, "save_project", lambda *args, **kwargs: saves.append(args))
    handler = _route_handler(route_module, "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-context/compile")
    request = DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene"},
        headers={"If-Match": "stale-header"},
        body={"base_modified_at": "stale-body" if body_stale else project.modified_at,
              "scene": scene.to_dict()})
    response = asyncio.run(route_module._project_error_middleware(request, handler))
    payload = _response_json(response)
    assert response.status == (409 if body_stale else 200)
    if body_stale:
        assert payload["code"] == "project_version_conflict"
        assert set(payload["project"]) == {
            "project_id", "modified_at",
            "prompt_semantic_units", "prompt_context_profiles",
        }
        assert payload["project"] == expected_projection
        assert payload["project"]["prompt_semantic_units"][0]["future_unit"] == {
            "nested": ["preserved"]}
        assert payload["project"]["prompt_context_profiles"][0]["future_profile"] == {
            "nested": {"preserved": True}}
        assert "assets" not in payload["project"]
        assert "scenes" not in payload["project"]
        assert response.headers["X-Sonder-Project-Modified-At"] == project.modified_at
        assert response.headers["X-Sonder-Project-Id"] == "proj"
    assert project.to_dict() == before
    assert saves == []
    # Other project mutations retain their header check.
    with pytest.raises(route_module.ProjectVersionConflict):
        route_module._load_project_from_request(request, repair_missing_frames=False)
    with pytest.raises(ValueError, match="cannot repair"):
        route_module._load_project_from_request(request, version_checked=False)


def test_compile_conflict_projection_does_not_serialize_full_project(
        monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    scene = Scene(scene_id="scene", duration_frames=20)
    raw_project = TimelineProject(
        project_id="proj", project_dir=str(project_dir), scenes=[scene]).to_dict()
    raw_project["prompt_semantic_units"] = [{
        "semantic_unit_id": "unit-1",
        "name": "Hero",
        "sources": [{
            "entity_id": "reference-1",
            "member_id": "member-1",
            "contribution": "",
            "future_source": {"nested": ["preserved"]},
        }],
        "future_unit": {"nested": {"preserved": True}},
    }]
    raw_project["prompt_context_profiles"] = [{
        "profile_id": "profile-1",
        "version": "1",
        "name": "Custom",
        "capabilities": {},
        "future_profile": {"nested": [1, 2]},
    }]
    project = TimelineProject.from_dict(raw_project, project_dir=str(project_dir))
    serialized = project.to_dict()
    expected = {
        "project_id": serialized["project_id"],
        "modified_at": serialized["modified_at"],
        "prompt_semantic_units": serialized["prompt_semantic_units"],
        "prompt_context_profiles": serialized["prompt_context_profiles"],
    }
    monkeypatch.setattr(
        route_module, "_load_project_from_request",
        lambda _request, **_kwargs: project)

    def reject_full_serialization(*_args, **_kwargs):
        raise AssertionError("stale conflict must not serialize the full project")

    monkeypatch.setattr(project, "to_dict", reject_full_serialization)
    handler = _route_handler(
        route_module, "POST",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-context/compile")
    request = DummyRequest(
        match_info={"project_id": "proj", "scene_id": "scene"},
        body={"base_modified_at": "stale", "scene": scene.to_dict()})

    response = asyncio.run(route_module._project_error_middleware(request, handler))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["project"] == expected
    assert set(payload["project"]) == {
        "project_id", "modified_at",
        "prompt_semantic_units", "prompt_context_profiles",
    }
    assert response.headers["X-Sonder-Project-Modified-At"] == project.modified_at
    assert response.headers["X-Sonder-Project-Id"] == "proj"
    assert payload["project"]["prompt_semantic_units"][0]["future_unit"] == {
        "nested": {"preserved": True}}
    assert payload["project"]["prompt_semantic_units"][0]["sources"][0][
        "future_source"] == {"nested": ["preserved"]}
    assert payload["project"]["prompt_context_profiles"][0]["future_profile"] == {
        "nested": [1, 2]}


@pytest.mark.parametrize("global_scope", [False, True])
def test_prompt_edit_merges_unrelated_changes_and_refuses_same_field(global_scope):
    section = PromptSection(0, 24, channels={"visual": "before", "speech": "old speech"})
    scene = Scene(scene_id="s", duration_frames=24, prompt_sections=[section])
    scene.set_global_channels({"visual": "before", "speech": "old speech"})
    project = TimelineProject(project_dir="", scenes=[scene])
    target = scene if global_scope else section
    docs_key = "global_channel_docs" if global_scope else "channel_docs"
    channels_key = "global_channels" if global_scope else "channels"
    original = copy.deepcopy(getattr(target, docs_key))
    fields = {"prompt_edit": {"documents": {"visual": {
        "expected": original["visual"], "value": prompt_context.text_document("mine")}}}}
    setter = target.set_global_channels if global_scope else target.set_channels
    setter({**getattr(target, channels_key), "speech": "someone else"})
    def apply():
        if global_scope:
            routes._apply_scene_fields(project, scene, fields)
        else:
            routes._apply_update_prompt_section(scene, 0, fields,
                {"prompt_id": section.prompt_id, "start_frame": 0, "end_frame": 24})
    apply()
    assert getattr(target, channels_key)["visual"] == "mine"
    assert getattr(target, channels_key)["speech"] == "someone else"
    before_refusal = target.to_dict()
    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        apply()
    assert raised.value.code == "prompt_edit_conflict"
    assert target.to_dict() == before_refusal


def test_prompt_edit_keeps_unrelated_chips_and_checks_template_and_stable_id():
    first = prompt_context.normalize_attachment({"attachment_id": "first", "kind": "custom"})
    other = prompt_context.normalize_attachment({"attachment_id": "other", "kind": "custom"})
    section = PromptSection(0, 24, channels={"visual": "scene"}, attachments=[first, other])
    scene = Scene(scene_id="s", duration_frames=24, prompt_sections=[section])
    project = TimelineProject(project_dir="", scenes=[scene])
    revised = copy.deepcopy(first); revised["config"]["text"] = "new"
    fields = {"prompt_edit": {"attachments": {"first": {"expected": first, "value": revised}}}}
    section.attachments[1]["config"]["text"] = "external"
    operation = {"type": "update_prompt_section", "index": 0, "fields": fields,
                 "expected": {"prompt_id": section.prompt_id},
                 "expected_prompt_template": routes.prompt_channel_templates.project_template_value(
                     routes.prompt_channel_templates.resolve_channel_template(project.metadata))}
    routes._apply_scene_mutation_operation(project, scene, operation)
    assert section.attachments[0]["config"]["text"] == "new"
    assert section.attachments[1]["config"]["text"] == "external"
    operation["expected"]["prompt_id"] = "replacement"
    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        routes._apply_scene_mutation_operation(project, scene, operation)
    assert raised.value.code == "identity_mismatch"
    operation["expected_prompt_template"] = "minimax_h3_ref"
    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        routes._apply_scene_mutation_operation(project, scene, operation)
    assert raised.value.code == "prompt_template_conflict"



def test_prompt_edit_accepts_sparse_expected_capability_after_server_normalization():
    """A second toggle owns the same chip even when the first save expanded it."""
    stored = prompt_context.normalize_attachment({
        "attachment_id": "chip", "kind": "reference",
        "capabilities": [{
            "capability_id": "summary", "kind": "summary", "enabled": False,
        }],
    })
    sparse_expected = copy.deepcopy(stored)
    sparse_expected["capabilities"] = [{
        "capability_id": "summary", "kind": "summary", "enabled": False,
    }]
    sparse_value = copy.deepcopy(sparse_expected)
    sparse_value["capabilities"][0]["enabled"] = True
    scene = Scene(scene_id="s", duration_frames=24,
                  global_attachments=[stored])
    project = TimelineProject(project_dir="", scenes=[scene])

    routes._apply_scene_fields(project, scene, {"prompt_edit": {
        "documents": {}, "attachments": {"chip": {
            "expected": sparse_expected, "value": sparse_value,
        }},
    }})

    assert scene.global_attachments[0]["capabilities"][0]["enabled"] is True
    assert set(scene.global_attachments[0]["capabilities"][0]) == {
        "capability_id", "kind", "channel_key", "placement", "config", "enabled",
    }



def test_prompt_edit_raw_equal_fast_path_does_not_invoke_normalizers(monkeypatch):
    """Equal durable records cannot acquire random ids during comparison."""
    document = prompt_context.normalize_prompt_document({
        "nodes": [{"type": "text", "node_id": "text", "text": "old"}],
    })
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "chip", "kind": "custom", "config": {"text": "old"},
    })

    def unexpected(*_args, **_kwargs):
        raise AssertionError("raw-equal prompt records must bypass normalization")

    monkeypatch.setattr(routes.prompt_context, "normalize_prompt_document", unexpected)
    monkeypatch.setattr(routes.prompt_context, "normalize_attachment", unexpected)
    fields = routes._merge_prompt_edit_fields({"prompt_edit": {
        "documents": {"visual": {"expected": document, "value": document}},
        "attachments": {"chip": {"expected": attachment, "value": attachment}},
    }}, {"visual": document}, [attachment])

    assert fields == {"channel_docs": {"visual": document},
                      "attachments": [attachment]}


def test_prompt_edit_conflict_response_names_only_the_contested_record():
    stored = prompt_context.normalize_attachment({
        "attachment_id": "chip", "kind": "custom", "config": {"text": "server"},
    })
    stale = copy.deepcopy(stored)
    stale["config"]["text"] = "stale"
    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        routes._merge_prompt_edit_fields({"prompt_edit": {
            "documents": {}, "attachments": {"chip": {
                "expected": stale, "value": stale,
            }},
        }}, {}, [stored])

    response = routes._mutation_json_error(raised.value)
    payload = json.loads(response.body)
    assert response.status == 409
    assert payload == {
        "error": "This prompt field changed elsewhere. Your draft was not saved.",
        "code": "prompt_edit_conflict",
        "conflict": {"record_kind": "attachment", "record_key": "chip",
                     "current": stored},
    }
    assert "project" not in payload
    assert "modified_at" not in payload

def test_prompt_edit_normalized_comparison_still_refuses_real_capability_change():
    stored = prompt_context.normalize_attachment({
        "attachment_id": "chip", "kind": "reference",
        "capabilities": [{
            "capability_id": "summary", "kind": "summary", "enabled": False,
        }],
    })
    stale = copy.deepcopy(stored)
    stale["capabilities"] = [{
        "capability_id": "summary", "kind": "summary", "enabled": True,
    }]
    scene = Scene(scene_id="s", duration_frames=24,
                  global_attachments=[stored])
    project = TimelineProject(project_dir="", scenes=[scene])

    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        routes._apply_scene_fields(project, scene, {"prompt_edit": {
            "documents": {}, "attachments": {"chip": {
                "expected": stale, "value": stale,
            }},
        }})

    assert raised.value.code == "prompt_edit_conflict"
    assert scene.global_attachments == [stored]


def test_generic_prompt_mutations_require_compare_state_and_reject_mixed_intents():
    section = PromptSection(0, 24, channels={"visual": "before"})
    scene = Scene(scene_id="s", duration_frames=24, prompt_sections=[section])
    scene.set_global_channels({"visual": "before"})
    project = TimelineProject(project_dir="", scenes=[scene])

    for operation in ({
        "type": "update_scene_fields",
        "fields": {"global_channels": {"visual": "mine"}},
    }, {
        "type": "update_prompt_section", "index": 0,
        "fields": {"channels": {"visual": "mine"}},
        "expected": {"prompt_id": section.prompt_id},
    }):
        with pytest.raises(routes.ProjectMutationRequestError) as raised:
            routes._apply_scene_mutation_operation(project, scene, operation)
        assert raised.value.code == "prompt_edit_expectation_required"

    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        routes._apply_scene_mutation_operation(project, scene, {
            "type": "update_scene_fields",
            "fields": {"global_channels": {"visual": "mine"},
                       "prompt_edit": {"documents": {}, "attachments": {}}},
            "expected": {"global_channels": {"visual": "before"}},
        })
    assert raised.value.code == "invalid_prompt_edit_intent"

    operation = {
        "type": "update_scene_fields",
        "fields": {"global_channels": {"visual": "mine"}},
        "expected": {"global_channels": {"visual": "before"}},
    }
    routes._apply_scene_mutation_operation(project, scene, operation)
    assert scene.global_channels["visual"] == "mine"
    scene.set_global_channels({"visual": "external"})
    with pytest.raises(routes.ProjectMutationRequestError) as raised:
        routes._apply_scene_mutation_operation(project, scene, operation)
    assert raised.value.code == "prompt_edit_conflict"
    assert scene.global_channels["visual"] == "external"


def test_profile_uncertain_create_verification_uses_exact_server_normalization(monkeypatch):
    module = _load_route_module(monkeypatch)
    project = TimelineProject(project_dir="")
    attempted = {"profile_id": "uncertain", "version": "1", "name": "Uncertain",
        "definition": {"compatible_templates": ["*"], "capabilities": {
            "custom": {"channel_key": "visual", "placement": "inline", "formatter": "{text}"}}}}
    project.prompt_context_profiles = module._create_prompt_context_profile(project, attempted)
    before = project.to_dict()
    def load(request, **kwargs):
        assert kwargs == {"repair_missing_frames": False,
                          "version_checked": False}
        return project
    monkeypatch.setattr(module, "_load_project_from_request", load)
    monkeypatch.setattr(module, "save_project", lambda *args, **kwargs: pytest.fail("verification wrote"))
    handler = _route_handler(module, "POST", "/sonder-editor/project/{project_id}/prompt-context/profiles/verify")
    response = asyncio.run(handler(DummyRequest(body=attempted)))
    assert _response_json(response)["matches"] is True
    changed = copy.deepcopy(attempted); changed["definition"]["capabilities"]["custom"]["formatter"] = "Other {text}"
    response = asyncio.run(handler(DummyRequest(body=changed)))
    assert _response_json(response)["matches"] is False
    assert project.to_dict() == before


# ---------------------------------------------------------------------------
# A durable-storage integrity failure is the server's problem, not the caller's.
# These four handlers each caught it through `ValueError` and answered 400 — three
# of them rendering a message that carries an absolute filesystem path. Driven per
# family rather than through one seam, because they reach storage by different
# entry points and only the family's own entry point proves its arm is gone.
# ---------------------------------------------------------------------------

STORAGE_SENTINEL_PATH = r"C:\__SECRET_ABSOLUTE_PATH__\state\ab.json"


def _storage_failure():
    from server.project_storage import ProjectStorageError
    return ProjectStorageError(
        f"provenance_ab: missing or unreadable component: {STORAGE_SENTINEL_PATH}")


_STORAGE_FAMILIES = {
    # The body must satisfy each route's own preconditions. A body that trips an
    # earlier guard produces the same `DID NOT RAISE` as an unfixed route, so the case
    # would read as proof of the defect while testing nothing — `asset_id is required`
    # is what the empty body for `permanent_delete` actually proved.
    "permanent_delete": ("POST", "/sonder-editor/project/{project_id}/assets/permanent",
                         "_apply_asset_delete_gesture", {"asset_id": "asset-1"}),
    "bulk_permanent_delete": ("POST", "/sonder-editor/project/{project_id}/assets/bulk-permanent-delete",
                              "_apply_asset_delete_gesture", {"asset_ids": ["asset-1"]}),
    "empty_trash": ("POST", "/sonder-editor/project/{project_id}/assets/empty-trash",
                    "_apply_asset_delete_gesture", {}),
    "profile_verify": ("POST", "/sonder-editor/project/{project_id}/prompt-context/profiles/verify",
                       "_load_project_from_request", {"content_hash": "x"}),
}


@pytest.mark.parametrize("family", sorted(_STORAGE_FAMILIES))
def test_storage_failure_is_never_answered_as_a_bad_request(monkeypatch, family):
    route_module = _load_route_module(monkeypatch)
    method, path, entry_point, body = _STORAGE_FAMILIES[family]
    reached = []

    def raise_storage_failure(*_args, **_kwargs):
        reached.append(entry_point)
        raise _storage_failure()

    monkeypatch.setattr(route_module, entry_point, raise_storage_failure)
    handler = _route_handler(route_module, method, path)
    request = DummyRequest(match_info={"project_id": "proj"}, method=method, body=body)

    with pytest.raises(route_module.ProjectStorageError) as caught:
        response = asyncio.run(handler(request))
        # Reported here rather than left to `DID NOT RAISE`, which cannot distinguish
        # "the route still claims the exception" from "the route never got that far".
        assert reached, (
            f"{family} returned {response.status} without reaching {entry_point}: "
            f"{_response_json(response)}")

    assert reached == [entry_point]
    # The message keeps the path deliberately — it goes to the log, never the client.
    assert STORAGE_SENTINEL_PATH in str(caught.value)


def test_remove_lane_refuses_when_the_lane_layout_moved_underneath(monkeypatch, tmp_path):
    """The retrofit: a stale lane index no longer deletes the wrong lane.

    A lane index is positional and this operation deletes, so the failure was
    silent and unrecoverable except through Undo. The guard is the family's lane
    count -- complete for `video`, `motion_driver` and `audio` because none of
    them is `lane_movable`, so nothing but an add or a remove can change what
    one of their indices means.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig(name="keep-0"), LaneConfig(name="keep-1"),
                                LaneConfig(name="doomed-2")]
    # On the lane a wrong-index removal would take, so the assertion below is
    # about data surviving rather than about a clip that was never at risk.
    scene.clips = [
        ClipReference(clip_id="on-2", timeline_start_frame=0, timeline_end_frame=5,
                      track_index=2),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])

    # The client read a four-lane scene; another writer has since removed one, so
    # index 2 no longer names the lane it named.
    saves = []
    refused = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "remove_lane", "lane_type": "video", "lane_index": 2,
        "item_policy": "delete_items",
        "expected": {"lane_count": 4, "config": {
            "name": "doomed-2", "color": "", "locked": False, "hidden": False}},
    }], saves)
    assert refused.status == 409
    assert _response_json(refused)["code"] == "identity_mismatch"
    assert saves == []
    assert scene.video_lane_count == 3
    assert [config.name for config in scene.video_lane_configs] == [
        "keep-0", "keep-1", "doomed-2"]

    # A guard that matches still removes the lane the caller meant.
    accepted = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "remove_lane", "lane_type": "video", "lane_index": 2,
        "item_policy": "delete_items",
        "expected": {"lane_count": 3, "config": {
            "name": "doomed-2", "color": "", "locked": False, "hidden": False}},
    }], saves)
    assert accepted.status == 200
    assert scene.video_lane_count == 2
    assert [config.name for config in scene.video_lane_configs] == ["keep-0", "keep-1"]
    # The clip on the deleted lane goes with it; the refusal above is what kept
    # it when the guard did not match.
    assert scene.clips == []


def test_remove_lane_requires_a_guard_rather_than_defaulting_to_none(monkeypatch, tmp_path):
    """Absence is refused, not tolerated.

    A caller that cannot send the guard -- a tab left open across a deploy -- is
    exactly the caller whose lane indices are most likely to be stale, so
    treating a missing guard as permission would exempt the worst case.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 2
    scene.video_lane_configs = [LaneConfig(), LaneConfig()]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])

    saves = []
    blank = {"name": "", "color": "", "locked": False, "hidden": False}
    for operation in ({"type": "remove_lane", "lane_type": "video", "lane_index": 1,
                       "item_policy": "require_empty"},
                      {"type": "remove_lane", "lane_type": "video", "lane_index": 1,
                       "item_policy": "require_empty", "expected": {}},
                      {"type": "remove_lane", "lane_type": "video", "lane_index": 1,
                       "item_policy": "require_empty",
                       "expected": {"lane_count": "2", "config": blank}},
                      {"type": "remove_lane", "lane_type": "video", "lane_index": 1,
                       "item_policy": "require_empty",
                       "expected": {"lane_count": True, "config": blank}},
                      # The config anchor is as mandatory as the count: without
                      # it the guard cannot see a permutation at equal count.
                      {"type": "remove_lane", "lane_type": "video", "lane_index": 1,
                       "item_policy": "require_empty",
                       "expected": {"lane_count": 2}}):
        response = _apply_scene_operations(
            route_module, monkeypatch, project, "scene-1", [operation], saves)
        assert response.status == 400, operation
        assert _response_json(response)["code"] == "missing_expected_identity"
    assert saves == []
    assert scene.video_lane_count == 2


def test_remove_lane_guards_a_reference_lane_by_its_durable_id(monkeypatch, tmp_path):
    """`reference` is the only movable family, so a count guard is not enough.

    Two lanes swapped by `move_lane` leave the count identical while every index
    means a different lane -- the case `durable_rules.md` names, and the reason
    this family carries its `lane_id` as well.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.reference_lane_count = 2
    scene.reference_lane_configs = [LaneConfig(), LaneConfig()]
    scene.reference_lane_recipes = [ReferenceLaneRecipe(lane_id="lane-a"),
                                    ReferenceLaneRecipe(lane_id="lane-b")]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    blank = {"name": "", "color": "", "locked": False, "hidden": False}

    saves = []
    swapped = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "remove_lane", "lane_type": "reference", "lane_index": 1,
        "item_policy": "require_empty",
        "expected": {"lane_count": 2, "config": blank, "lane_id": "lane-a"},
    }], saves)
    assert swapped.status == 409
    assert _response_json(swapped)["code"] == "identity_mismatch"
    assert saves == []
    assert scene.reference_lane_count == 2

    matched = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "remove_lane", "lane_type": "reference", "lane_index": 1,
        "item_policy": "require_empty",
        "expected": {"lane_count": 2, "config": blank, "lane_id": "lane-b"},
    }], saves)
    assert matched.status == 200
    assert [recipe.lane_id for recipe in scene.reference_lane_recipes] == ["lane-a"]


def test_remove_lane_tolerates_a_reference_lane_with_no_durable_id(monkeypatch, tmp_path):
    """The bootstrap case `_apply_lane_config` already tolerates.

    A lane whose recipe was never written has no id the caller could have read,
    so demanding one would refuse a legitimate removal. The count still covers it.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.reference_lane_count = 2
    scene.reference_lane_configs = [LaneConfig(), LaneConfig()]
    scene.reference_lane_recipes = [ReferenceLaneRecipe(lane_id="lane-a")]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])

    saves = []
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "remove_lane", "lane_type": "reference", "lane_index": 1,
        "item_policy": "require_empty",
        "expected": {"lane_count": 2, "config": {
            "name": "", "color": "", "locked": False, "hidden": False}},
    }], saves)
    assert response.status == 200
    assert scene.reference_lane_count == 1


def test_consolidate_items_still_vacates_lanes_it_emptied(monkeypatch, tmp_path):
    """The internal removal is server-derived and must not need a client guard.

    `_consolidate_media_items` removes a lane it has just emptied, from state it
    is already holding. Making `expected` mandatory without exempting this path
    would have broken every consolidation that removes vacated lanes -- which is
    what `LANE_INDEX_FROM_SERVER_STATE` exists to say out loud.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig(), LaneConfig(), LaneConfig()]
    scene.clips = [
        ClipReference(clip_id="a", timeline_start_frame=0, timeline_end_frame=5,
                      track_index=0),
        ClipReference(clip_id="b", timeline_start_frame=10, timeline_end_frame=15,
                      track_index=2),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])

    saves = []
    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "consolidate_items", "lane_type": "video",
        "item_ids": ["a", "b"], "target_lane": 0, "remove_vacated_lanes": True,
    }], saves)
    assert response.status == 200, _response_json(response)
    assert _response_json(response)["results"][0]["removed_lanes"] == [2]
    assert scene.video_lane_count == 2
    assert {clip.track_index for clip in scene.clips} == {0}


def test_create_guide_refuses_replacing_a_guide_the_caller_never_saw(monkeypatch, tmp_path):
    """The retrofit: replacement is deliberate, or it is refused.

    `_apply_create_guide` drops every guide already at the frame. That is a
    two-sided semantic the author can see — the client mirrors it — so the guard
    names what is being replaced rather than demanding an empty frame. Only the
    guide this client could not see is refused.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.guide_frames = [GuideFrame(guide_id="other-tab", frame_index=12,
                                     asset_id="asset-a")]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])

    # The client believed frame 12 was empty; another writer put a guide there.
    saves = []
    blind = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "create_guide",
        "fields": {"guide_id": "mine", "frame_index": 12, "asset_id": "asset-b"},
        "expected": {"replaces_guide_id": ""},
    }], saves)
    assert blind.status == 409
    assert _response_json(blind)["code"] == "identity_mismatch"
    assert saves == []
    assert [guide.guide_id for guide in scene.guide_frames] == ["other-tab"]

    # Naming the occupant is the deliberate replacement, and it still works.
    deliberate = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "create_guide",
        "fields": {"guide_id": "mine", "frame_index": 12, "asset_id": "asset-b"},
        "expected": {"replaces_guide_id": "other-tab"},
    }], saves)
    assert deliberate.status == 200
    assert [guide.guide_id for guide in scene.guide_frames] == ["mine"]

    # And an empty frame accepts the empty claim.
    fresh = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "create_guide",
        "fields": {"guide_id": "second", "frame_index": 40, "asset_id": "asset-c"},
        "expected": {"replaces_guide_id": ""},
    }], saves)
    assert fresh.status == 200
    assert {guide.guide_id for guide in scene.guide_frames} == {"mine", "second"}


def test_create_guide_requires_the_replacement_claim(monkeypatch, tmp_path):
    """Absence is refused. A caller that cannot say is the one most likely stale."""
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])

    saves = []
    for operation in ({"type": "create_guide",
                       "fields": {"frame_index": 3, "asset_id": "a"}},
                      {"type": "create_guide",
                       "fields": {"frame_index": 3, "asset_id": "a"},
                       "expected": {}},
                      {"type": "create_guide",
                       "fields": {"frame_index": 3, "asset_id": "a"},
                       "expected": {"guide_id": "x"}}):
        response = _apply_scene_operations(
            route_module, monkeypatch, project, "scene-1", [operation], saves)
        assert response.status == 400, operation
        assert _response_json(response)["code"] == "missing_expected_identity"
    assert saves == []
    assert scene.guide_frames == []


def test_remove_lane_catches_a_permutation_that_leaves_the_count_unchanged(
        monkeypatch, tmp_path):
    """The counterexample that disproved the first version of this guard.

    A remove plus an add restores the lane count while shifting every index
    above the removal, so a count-equality guard accepted a stale removal and
    destroyed a clip on a lane the caller never named. The config anchor --
    the identity `durable_rules.md` names for families with no durable id, and
    the one `findLaneByAnchor` already resolves by on the client -- is what
    catches it.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig(name="v0"), LaneConfig(name="v1"),
                                LaneConfig(name="v2")]
    scene.clips = [
        ClipReference(clip_id="on-0", timeline_start_frame=0, timeline_end_frame=5,
                      track_index=0),
        ClipReference(clip_id="on-2", timeline_start_frame=0, timeline_end_frame=5,
                      track_index=2),
    ]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    # What client A captured while looking at a three-lane scene: it means the
    # lane called "v1", which is at index 1.
    authored = {"lane_count": 3, "config": {
        "name": "v1", "color": "", "locked": False, "hidden": False}}

    # Client B removes lane 0 and then adds a lane. Net zero on the count;
    # every index above 0 has shifted down and a blank lane is appended.
    assert _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "remove_lane", "lane_type": "video", "lane_index": 0,
        "item_policy": "delete_items",
        "expected": {"lane_count": 3, "config": {
            "name": "v0", "color": "", "locked": False, "hidden": False}},
    }], saves).status == 200
    assert _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "set_lane_count", "lane_type": "video", "count": 3,
    }], saves).status == 200
    assert [config.name for config in scene.video_lane_configs] == ["v1", "v2", ""]
    assert scene.video_lane_count == 3

    # A's stale removal must not delete "v2" -- and must not take `on-2` with it.
    saves.clear()
    refused = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "remove_lane", "lane_type": "video", "lane_index": 1,
        "item_policy": "delete_items", "expected": authored,
    }], saves)
    assert refused.status == 409
    assert _response_json(refused)["code"] == "identity_mismatch"
    assert saves == []
    assert [config.name for config in scene.video_lane_configs] == ["v1", "v2", ""]
    assert {clip.clip_id for clip in scene.clips} == {"on-2"}


def test_remove_lane_accepts_a_concurrent_lane_ADD(monkeypatch, tmp_path):
    """An append moves nothing, so refusing it would be a pure false refusal.

    `_set_scene_lane_count` grows by appending and `remove_lane` deletes in
    place, so a family that only grew cannot have moved this caller's lane. The
    count is therefore compared as a floor, never for equality -- the first
    version demanded equality and refused this, which costs the user an edit for
    no protection at all.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.video_lane_count = 3
    scene.video_lane_configs = [LaneConfig(name="v0"), LaneConfig(name="v1"),
                                LaneConfig(name="v2")]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    # Another writer appends a lane while this removal is in flight.
    assert _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "set_lane_count", "lane_type": "video", "count": 4,
    }], saves).status == 200

    accepted = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "remove_lane", "lane_type": "video", "lane_index": 1,
        "item_policy": "require_empty",
        "expected": {"lane_count": 3, "config": {
            "name": "v1", "color": "", "locked": False, "hidden": False}},
    }], saves)
    assert accepted.status == 200
    assert [config.name for config in scene.video_lane_configs] == ["v0", "v2", ""]


def test_create_guide_cannot_name_one_of_two_occupants(monkeypatch, tmp_path):
    """`_apply_create_guide` deletes EVERY guide at the frame, so the claim must
    cover every one of them.

    Duplicates cannot be produced by `create_guide` or `move_guide` -- both
    replace at the frame -- but deserialization does not dedupe, so a
    hand-edited or imported document can hold them. Naming one of two would
    authorize destroying the other with nothing compared.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.guide_frames = [GuideFrame(guide_id="first", frame_index=12),
                          GuideFrame(guide_id="second", frame_index=12)]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])

    saves = []
    for claim in ("first", "second", ""):
        response = _apply_scene_operations(
            route_module, monkeypatch, project, "scene-1", [{
                "type": "create_guide",
                "fields": {"guide_id": "mine", "frame_index": 12},
                "expected": {"replaces_guide_id": claim},
            }], saves)
        assert response.status == 409, claim
        assert _response_json(response)["code"] == "identity_mismatch"
    assert saves == []
    assert {guide.guide_id for guide in scene.guide_frames} == {"first", "second"}


def test_move_guide_refuses_a_destination_the_caller_never_saw(monkeypatch, tmp_path):
    """The same blind-replacement defect `create_guide` was retrofitted for.

    `_apply_move_guide` deletes whatever sits on the destination frame and
    rewrites its link groups, and its `expected` only identified the guide being
    MOVED. Found by an adversarial audit of the `create_guide` retrofit: guarding
    one door of a two-door room.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.guide_frames = [GuideFrame(guide_id="mine", frame_index=5, asset_id="a"),
                          GuideFrame(guide_id="other-tab", frame_index=8, asset_id="b")]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    blind = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "move_guide", "from_frame_index": 5, "to_frame_index": 8,
        "expected": {"frame_index": 5, "guide_id": "mine",
                     "replaces_guide_id": ""},
    }], saves)
    assert blind.status == 409
    assert _response_json(blind)["code"] == "identity_mismatch"
    assert saves == []
    assert {(guide.guide_id, guide.frame_index) for guide in scene.guide_frames} == {
        ("mine", 5), ("other-tab", 8)}

    # A drag that ends where it began: the guide being moved occupies its own
    # destination, and naming itself as what it replaces would be nonsense.
    same = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "move_guide", "from_frame_index": 5, "to_frame_index": 5,
        "expected": {"frame_index": 5, "guide_id": "mine",
                     "replaces_guide_id": ""},
    }], saves)
    assert same.status == 200, _response_json(same)

    # And a guard with no destination claim at all is refused, not defaulted.
    missing = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "move_guide", "from_frame_index": 5, "to_frame_index": 8,
        "expected": {"frame_index": 5, "guide_id": "mine"},
    }], saves)
    assert missing.status == 400
    assert _response_json(missing)["code"] == "missing_expected_identity"


def test_create_reference_item_refuses_an_extent_measured_against_a_moved_lane(
        monkeypatch, tmp_path):
    """The half `_require_no_reference_overlap` does not cover.

    Both emitters set `end_frame` to the start of the next item on the lane,
    read out of their own copy. An item appearing EARLIER makes the extent
    overlap and is already refused. An item being DELETED, or moved later,
    leaves the new item shorter than it was drawn — no overlap, no refusal, and
    a silently wrong extent.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.duration_frames = 200
    scene.reference_lane_count = 1
    scene.reference_lane_configs = [LaneConfig()]
    scene.reference_lane_recipes = [ReferenceLaneRecipe(lane_id="lane-a")]
    scene.reference_items = [ReferenceItem(reference_item_id="later", lane_index=0,
                                           start_frame=100, end_frame=150)]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    # A Reference item needs at least one Library member to exist at all, so the
    # accepted arm below has something real to stage.
    project.references = [ReferenceEntity(
        reference_id="ref", name="Subject",
        members=[ReferenceMember(member_id="member", asset_id="asset",
                                 handle="SUBJECT")])]
    project.assets = [Asset(asset_id="asset", asset_type="image",
                            path="media/subject.png")]
    member = [{"entity_id": "ref", "member_id": "member"}]
    saves = []

    # The client drew a bar from 10 up to the item at 100. That item is then
    # deleted, so the extent it measured no longer describes the lane.
    scene.reference_items = []
    stale = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "create_reference_item",
        "expected": {"next_start_frame": 100},
        "fields": {"lane_index": 0, "start_frame": 10, "end_frame": 100,
                   "members": member},
    }], saves)
    assert stale.status == 409
    assert _response_json(stale)["code"] == "identity_mismatch"
    assert saves == []
    assert scene.reference_items == []

    # With a real later item present and named, the same create succeeds. The
    # later item matters: an earlier version of this test emptied the lane
    # first, so `_next_reference_start_after` was never consulted and a helper
    # that always returned -1 would have passed every arm.
    scene.reference_items = [ReferenceItem(reference_item_id="later", lane_index=0,
                                           start_frame=100, end_frame=150)]
    accepted = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "create_reference_item",
        "expected": {"next_start_frame": 100},
        "fields": {"lane_index": 0, "start_frame": 10, "end_frame": 100,
                   "members": member},
    }], saves)
    assert accepted.status == 200, _response_json(accepted)
    assert sorted(item.start_frame for item in scene.reference_items) == [10, 100]

    # An item on ANOTHER lane must not be what the guard measures against.
    scene.reference_lane_count = 2
    scene.reference_lane_configs.append(LaneConfig())
    scene.reference_lane_recipes.append(ReferenceLaneRecipe(lane_id="lane-b"))
    other_lane = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "create_reference_item",
        "expected": {"next_start_frame": -1},
        "fields": {"lane_index": 1, "start_frame": 10, "end_frame": -1,
                   "members": member},
    }], saves)
    assert other_lane.status == 200, _response_json(other_lane)

    # And a missing claim is refused rather than defaulted.
    missing = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "create_reference_item",
        "fields": {"lane_index": 0, "start_frame": 200, "end_frame": -1,
                   "members": member},
    }], saves)
    assert missing.status == 400
    assert _response_json(missing)["code"] == "missing_expected_identity"


def test_replace_prompt_sections_refuses_a_collection_that_moved(monkeypatch, tmp_path):
    """A wholesale replace discards every concurrent edit without a word."""
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.prompt_sections = [PromptSection(start_frame=0, end_frame=10),
                             PromptSection(start_frame=10, end_frame=20)]
    scene.prompt_sections[0].prompt_id = "p1"
    scene.prompt_sections[1].prompt_id = "p2"
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    replacement = [{"prompt_id": "new", "start_frame": 0, "end_frame": 30}]

    # Another writer split, removed or retimed a section meanwhile.
    for stale in (
            [{"prompt_id": "p1", "start_frame": 0, "end_frame": 10}],
            [{"prompt_id": "p1", "start_frame": 0, "end_frame": 10},
             {"prompt_id": "p2", "start_frame": 10, "end_frame": 25}],
            [{"prompt_id": "p2", "start_frame": 10, "end_frame": 20},
             {"prompt_id": "p1", "start_frame": 0, "end_frame": 10}],
    ):
        response = _apply_scene_operations(
            route_module, monkeypatch, project, "scene-1", [{
                "type": "replace_prompt_sections", "sections": replacement,
                "expected": {"sections": stale},
            }], saves)
        assert response.status == 409, stale
        assert _response_json(response)["code"] == "identity_mismatch"
    assert saves == []
    assert [section.prompt_id for section in scene.prompt_sections] == ["p1", "p2"]

    # A missing claim is refused, not defaulted to permission.
    missing = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "replace_prompt_sections", "sections": replacement,
    }], saves)
    assert missing.status == 400
    assert _response_json(missing)["code"] == "missing_expected_identity"

    # The collection as the caller saw it still applies.
    accepted = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "replace_prompt_sections", "sections": replacement,
        "expected": {"sections": [
            {"prompt_id": "p1", "start_frame": 0, "end_frame": 10},
            {"prompt_id": "p2", "start_frame": 10, "end_frame": 20},
        ]},
    }], saves)
    assert accepted.status == 200, _response_json(accepted)
    assert [section.prompt_id for section in scene.prompt_sections] == ["new"]


def test_bulk_delete_honours_its_per_item_guard_on_the_linked_path(
        monkeypatch, tmp_path):
    """A guarded delete must not become an unguarded one by being linked.

    `_item_ref_from_selection` resolves a prompt by LIST POSITION and reads no
    `expected`, so on the `apply_linked` path the per-item snapshot the client
    already builds was thrown away — and a stale index deleted whatever row had
    inherited it.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.prompt_sections = [PromptSection(start_frame=0, end_frame=5),
                             PromptSection(start_frame=5, end_frame=10)]
    scene.prompt_sections[0].prompt_id = "inserted"
    scene.prompt_sections[1].prompt_id = "mine"
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    # The caller selected the section at index 0 when it was "mine"; another
    # writer has since inserted a section ahead of it.
    refused = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "bulk_delete_items", "apply_linked": True,
        "items": [{"type": "prompt", "id": 0,
                   "expected": {"prompt_id": "mine", "start_frame": 5,
                                "end_frame": 10}}],
    }], saves)
    assert refused.status == 409
    assert _response_json(refused)["code"] == "identity_mismatch"
    assert saves == []
    assert [section.prompt_id for section in scene.prompt_sections] == [
        "inserted", "mine"]

    # Naming the row that is really there deletes that row and no other.
    accepted = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "bulk_delete_items", "apply_linked": True,
        "items": [{"type": "prompt", "id": 1,
                   "expected": {"prompt_id": "mine", "start_frame": 5,
                                "end_frame": 10}}],
    }], saves)
    assert accepted.status == 200, _response_json(accepted)
    assert [section.prompt_id for section in scene.prompt_sections] == ["inserted"]


def test_bulk_delete_linked_still_accepts_durable_members_with_no_snapshot(
        monkeypatch, tmp_path):
    """Clip and audio members carry no `expected` and must not start needing one.

    `_mutationItemFromSelection` builds a snapshot for guide, prompt and
    reference items only — the other two are addressed by durable id, so there
    is nothing to compare and demanding it would refuse every linked delete.
    """
    route_module = _load_route_module(monkeypatch)
    scene = Scene(scene_id="scene-1", name="Scene")
    scene.clips = [ClipReference(clip_id="clip-a", timeline_start_frame=0,
                                 timeline_end_frame=10, track_index=0)]
    scene.audio_tracks = [AudioTrack(track_id="audio-a", timeline_start_frame=0,
                                     timeline_end_frame=10, lane_index=0)]
    project = TimelineProject(project_dir=str(tmp_path), name="Project", scenes=[scene])
    saves = []

    response = _apply_scene_operations(route_module, monkeypatch, project, "scene-1", [{
        "type": "bulk_delete_items", "apply_linked": True,
        "items": [{"type": "clip", "id": "clip-a"},
                  {"type": "audio", "id": "audio-a"}],
    }], saves)
    assert response.status == 200, _response_json(response)
    assert scene.clips == []
    assert scene.audio_tracks == []
