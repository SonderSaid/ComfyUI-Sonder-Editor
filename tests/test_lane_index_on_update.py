"""A clip or audio update names a lane that exists (backlog step 1, Phase 3, #28).

`update_clip` and `update_audio_track` never range-checked the lane index they
were given. The lock check they ran instead, `_require_lane_unlocked`, reaches
`_lane_config`, which PADS the config list out to whatever index it is asked
about, so an update to lane 7 of a two-lane scene was accepted, stored the item
on a lane nothing draws, and grew the config list behind it. A `role` change
moved a clip into the other lane family at the same index without asking
whether that family has the lane at all.

Create already refuses an index out of range (`_apply_create_clip`,
`404 item_not_found`); update now gives the same answer, before any lock check,
so a refusal leaves the config list exactly as it was.
"""

import asyncio
import importlib
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server import routes
from server.timeline_state import (
    Asset,
    AudioTrack,
    ClipReference,
    LaneConfig,
    Scene,
    TimelineProject,
)


def _project():
    scene = Scene(
        scene_id="scene-1", duration_frames=100,
        video_lane_count=2, video_lane_configs=[LaneConfig(), LaneConfig()],
        motion_driver_lane_count=1, motion_driver_lane_configs=[LaneConfig()],
        audio_lane_count=2, audio_lane_configs=[LaneConfig(), LaneConfig()],
        clips=[ClipReference(clip_id="clip-1", source_path="media/v.mp4",
                             track_index=1, role="render",
                             timeline_start_frame=0, timeline_end_frame=20)],
        audio_tracks=[AudioTrack(track_id="audio-1", source_path="media/a.wav",
                                 lane_index=1, timeline_start_frame=0, timeline_end_frame=20)])
    assets = [Asset(asset_id="video", asset_type="video", path="media/v.mp4",
                    frame_count=100, duration_sec=4.0)]
    return TimelineProject(project_id="p", fps=24.0, scenes=[scene], assets=assets)


def _config_lengths(scene):
    return (len(scene.video_lane_configs), len(scene.motion_driver_lane_configs),
            len(scene.audio_lane_configs))


def _refused(project, operation):
    scene = project.get_scene("scene-1")
    before = _config_lengths(scene)
    with pytest.raises(routes.ProjectMutationRequestError) as caught:
        routes._apply_scene_mutation_batch(project, "scene-1", [operation])
    assert _config_lengths(scene) == before, "a refusal padded the config list"
    return caught.value


def _clip(project):
    return project.get_scene("scene-1").clips[0]


def _audio(project):
    return project.get_scene("scene-1").audio_tracks[0]


@pytest.mark.parametrize("track_index", [2, 7, -1])
def test_a_clip_update_to_a_missing_video_lane_is_refused(track_index):
    project = _project()
    error = _refused(project, {"type": "update_clip", "clip_id": "clip-1",
                               "fields": {"track_index": track_index}})
    assert (error.status, error.code) == (404, "item_not_found")
    assert "out of range" in str(error)
    assert _clip(project).track_index == 1


def test_a_role_only_change_into_a_family_without_that_lane_is_refused():
    """Video lane 1 exists; Driver lane 1 does not."""
    project = _project()
    error = _refused(project, {"type": "update_clip", "clip_id": "clip-1",
                               "fields": {"role": "motion_driver"}})
    assert (error.status, error.code) == (404, "item_not_found")
    assert (_clip(project).role, _clip(project).track_index) == ("render", 1)


def test_a_role_change_with_a_lane_the_family_has_is_accepted():
    project = _project()
    routes._apply_scene_mutation_batch(project, "scene-1", [
        {"type": "update_clip", "clip_id": "clip-1",
         "fields": {"role": "motion_driver", "track_index": 0}}])
    assert (_clip(project).role, _clip(project).track_index) == ("motion_driver", 0)


def test_a_clip_update_within_range_is_accepted():
    project = _project()
    routes._apply_scene_mutation_batch(project, "scene-1", [
        {"type": "update_clip", "clip_id": "clip-1", "fields": {"track_index": 0}}])
    assert _clip(project).track_index == 0


def test_a_clip_update_naming_no_lane_is_not_range_checked():
    """A stored clip on a lane that is gone still takes a scalar edit."""
    project = _project()
    _clip(project).track_index = 5
    routes._apply_scene_mutation_batch(project, "scene-1", [
        {"type": "update_clip", "clip_id": "clip-1", "fields": {"opacity": 0.5}}])
    assert _clip(project).opacity == 0.5


def test_an_out_of_range_lane_is_refused_before_the_lock_check():
    """A locked current lane would answer 409 and hide the real cause."""
    project = _project()
    project.get_scene("scene-1").video_lane_configs[1].locked = True
    error = _refused(project, {"type": "update_clip", "clip_id": "clip-1",
                               "fields": {"track_index": 9}})
    assert error.code == "item_not_found"


def test_an_unreadable_clip_lane_is_a_400():
    error = _refused(_project(), {"type": "update_clip", "clip_id": "clip-1",
                                  "fields": {"track_index": "two"}})
    assert error.status == 400


@pytest.mark.parametrize("lane_index", [2, 7, -1])
def test_an_audio_update_to_a_missing_lane_is_refused(lane_index):
    project = _project()
    error = _refused(project, {"type": "update_audio_track", "track_id": "audio-1",
                               "fields": {"lane_index": lane_index}})
    assert (error.status, error.code) == (404, "item_not_found")
    assert _audio(project).lane_index == 1


def test_an_audio_update_within_range_is_accepted():
    project = _project()
    routes._apply_scene_mutation_batch(project, "scene-1", [
        {"type": "update_audio_track", "track_id": "audio-1", "fields": {"lane_index": 0}}])
    assert _audio(project).lane_index == 0


def test_an_unreadable_audio_lane_is_a_400():
    error = _refused(_project(), {"type": "update_audio_track", "track_id": "audio-1",
                                  "fields": {"lane_index": "two"}})
    assert error.status == 400


# -- the legacy audio PUT -----------------------------------------------------------

def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(
        instance=SimpleNamespace(routes=web.RouteTableDef(), app=web.Application()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


class _Request(dict):
    def __init__(self, *, match_info=None, body=None):
        super().__init__()
        self.match_info = match_info or {}
        self.query = {}
        self.headers = {}
        self._body = body

    async def json(self):
        return self._body


def _legacy_audio_put(monkeypatch, project, body):
    route_module = _load_route_module(monkeypatch)
    saves = []
    monkeypatch.setattr(route_module, "_load_project_from_request", lambda _request, **_kwargs: project)
    monkeypatch.setattr(route_module, "save_project",
                        lambda saved, **_kwargs: saves.append(saved))
    path = "/sonder-editor/project/{project_id}/scenes/{scene_id}/audio_tracks/{track_id}"
    handler = next(route.handler for route in route_module.routes
                   if route.method == "PUT" and route.path == path)
    response = asyncio.run(handler(_Request(
        match_info={"project_id": "p", "scene_id": "scene-1", "track_id": "audio-1"},
        body=body)))
    return response, json.loads(response.body.decode("utf-8")), saves


def test_the_legacy_audio_put_refuses_a_missing_lane_without_padding(monkeypatch):
    project = _project()
    scene = project.get_scene("scene-1")
    response, payload, saves = _legacy_audio_put(monkeypatch, project, {"lane_index": 6})
    assert (response.status, payload["code"], saves) == (404, "item_not_found", [])
    assert len(scene.audio_lane_configs) == 2
    assert _audio(project).lane_index == 1


def test_the_legacy_audio_put_moves_within_range(monkeypatch):
    project = _project()
    response, payload, saves = _legacy_audio_put(monkeypatch, project, {"lane_index": 0})
    assert (response.status, payload["lane_index"], len(saves)) == (200, 0, 1)
