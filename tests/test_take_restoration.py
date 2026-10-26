"""Restoring a take's placement when it is dropped from Assets (take placement Phase 3).

The geometry contract lives in `fixtures/take_restoration_cases.json`; the route
tests drive `_apply_scene_mutation_batch`, the in-memory half of the scene
mutations route, so batch-level behavior (the lane a batch adds and removes
again) is exercised as one batch.
"""
import copy
import json
from pathlib import Path

import pytest

from server import project_manager as pm, project_storage as ps
from server import routes
from server.take_placement import restore_take_geometry, take_restoration_source
from server.timeline_state import (
    Asset, ClipReference, AudioTrack, LaneConfig, Scene, TimelineProject, effective_scene_fps,
)

CASES = json.loads((Path(__file__).parent / "fixtures" / "take_restoration_cases.json").read_text(encoding="utf-8"))
GEOMETRY_KEYS = ("source_in_frame", "source_out_frame", "total_source_frames",
                 "timeline_start_frame", "timeline_end_frame")


def _params(case):
    base = case["params"]
    params = copy.deepcopy(CASES["params"][base] if isinstance(base, str) else base)
    for key in case.get("remove", ()):
        params.pop(key, None)
    params.update(copy.deepcopy(case.get("override", {})))
    return params


@pytest.mark.parametrize("case", CASES["sources"], ids=lambda case: case["name"])
def test_source_resolution_matches_the_contract(case):
    source = take_restoration_source(_params(case), case["asset_type"])
    for key, value in case["expect"].items():
        assert source.get(key) == value, (key, source)


@pytest.mark.parametrize("case", CASES["geometry"], ids=lambda case: case["name"])
def test_restored_geometry_matches_the_contract(case):
    params = _params(case)
    source = take_restoration_source(params, "video")
    assert source["status"] == "supported"
    geometry = restore_take_geometry(source, case["encoded"], same_scene=case["same_scene"],
                                     destination_fps=case["fps"], cursor_frame=case["cursor"])
    if "reason" in case:
        assert geometry == {"reason": case["reason"]}
        return
    assert [geometry[key] for key in GEOMETRY_KEYS] == case["expect"]
    assert geometry["source_origin_frame"] == 0
    # One duration: the timeline span IS the converted source span.
    assert (geometry["timeline_end_frame"] - geometry["timeline_start_frame"]
            == geometry["source_out_frame"] - geometry["source_in_frame"])


def test_the_plan_pin_never_rounds_two_durations():
    """24->30: timeline [20,33) and source [17,30) independently round to 16 and 17."""
    source = take_restoration_source(CASES["params"]["base"], "video")
    geometry = restore_take_geometry(source, 40, same_scene=True, destination_fps=30, cursor_frame=0)
    independent = (round(33 * 1.25) - round(20 * 1.25), 38 - 21)
    assert independent == (16, 17)
    assert geometry["timeline_end_frame"] - geometry["timeline_start_frame"] == 17


# -- route level ----------------------------------------------------------------

BASE = CASES["params"]["base"]


@pytest.fixture
def take_project(tmp_path, monkeypatch):
    media = tmp_path / "media"
    media.mkdir()
    (media / "take.mp4").write_bytes(b"video" * 400)
    (media / "take_audio.wav").write_bytes(b"audio" * 400)
    video = Asset(asset_id="v1", name="take.mp4", asset_type="video", path="media/take.mp4",
                  frame_count=40, fps=24.0, duration_sec=40 / 24, has_audio=True,
                  media_probe_signature="2000:1", generation_params=copy.deepcopy(BASE))
    audio = Asset(asset_id="a1", name="take.mp4 (audio)", asset_type="audio",
                  path="media/take_audio.wav", duration_sec=40 / 24, sample_rate=48000,
                  generation_params={**copy.deepcopy(BASE), "encoded_frame_count": 40,
                                     "source_video_asset_id": "v1"})
    s1 = Scene(scene_id="s1", name="One", fps=24.0, duration_frames=96)
    s2 = Scene(scene_id="s2", name="Two", fps=30.0, duration_frames=120)
    project = TimelineProject(project_dir=str(tmp_path), name="Takes", scenes=[s1, s2])
    project.assets = [video, audio]
    calls = {"extract": 0}

    def extract(_video_path, output_path):
        calls["extract"] += 1
        Path(output_path).write_bytes(b"x" * 4096)
        return True

    monkeypatch.setattr(routes, "_extract_audio_from_video", extract)
    monkeypatch.setattr(routes, "_get_audio_duration", lambda *_a, **_k: 40 / 24)
    monkeypatch.setattr(routes, "ensure_thumbnail", lambda *a, **k: None)
    return project, calls


def _expected(project, scene, asset, geometry):
    return {
        "asset_id": asset.asset_id,
        "provenance_revision": routes._asset_summary_and_revision(asset)[1],
        "path": asset.path,
        "media_probe_signature": asset.media_probe_signature,
        "frame_count": asset.frame_count,
        "duration_sec": asset.duration_sec,
        "media_replaced_at": asset.media_replaced_at,
        "scene_fps": effective_scene_fps(project, scene),
        **{key: geometry[key] for key in GEOMETRY_KEYS if key != "timeline_start_frame"},
    }


def _geometry(values):
    return dict(zip(GEOMETRY_KEYS, values))


def _ruler_restore(project, scene, asset, geometry, *, dual=True, expected=None):
    """The batch a ruler drop sends: new lanes, then the restoring create."""
    ops = [{"type": "set_lane_count", "lane_type": "video",
            "count": scene.video_lane_count + 1}]
    if dual:
        ops.append({"type": "set_lane_count", "lane_type": "audio",
                    "count": scene.audio_lane_count + 1})
    ops.append({
        "type": "create_clip",
        "fields": {"asset_id": asset.asset_id, "timeline_start_frame": geometry["timeline_start_frame"],
                   "track_index": scene.video_lane_count, "audio_lane_index": scene.audio_lane_count,
                   "dual_drop": dual, "link_video_audio": True, "restore_take": True},
        "expected": expected or _expected(project, scene, asset, geometry),
    })
    return routes._apply_scene_mutation_batch(project, scene.scene_id, ops)


def _refusal(callable_):
    with pytest.raises(routes.ProjectMutationRequestError) as error:
        callable_()
    return error.value


def test_same_scene_restores_original_time_with_the_takes_own_audio(take_project):
    project, calls = take_project
    scene = project.get_scene("s1")
    geometry = _geometry([17, 30, 40, 20, 33])
    _committed, payload = _ruler_restore(project, scene, project.get_asset("v1"), geometry)
    result = payload["results"][-1]
    clip, track = scene.clips[0], scene.audio_tracks[0]
    assert [getattr(clip, key) for key in GEOMETRY_KEYS] == [17, 30, 40, 20, 33]
    assert clip.source_origin_frame == 0 and clip.track_index == 1
    assert track.source_path == "media/take_audio.wav" and track.lane_index == 1
    assert (track.source_in_frame, track.total_source_frames,
            track.timeline_start_frame, track.timeline_end_frame) == (17, 40, 20, 33)
    assert len(scene.linked_item_groups) == 1
    assert result["take_restoration"] == {"same_scene": True, "audio_source": "original", "warnings": []}
    assert calls["extract"] == 0


def test_another_scene_places_the_trim_at_the_cursor_in_its_own_clock(take_project):
    project, _calls = take_project
    scene = project.get_scene("s2")
    geometry = _geometry([21, 38, 50, 100, 117])
    _committed, payload = _ruler_restore(project, scene, project.get_asset("v1"), geometry)
    clip, track = scene.clips[0], scene.audio_tracks[0]
    assert [getattr(clip, key) for key in GEOMETRY_KEYS] == [21, 38, 50, 100, 117]
    assert (track.source_in_frame, track.total_source_frames,
            track.timeline_start_frame, track.timeline_end_frame) == (21, 50, 100, 117)
    assert payload["results"][-1]["take_restoration"]["same_scene"] is False


@pytest.mark.parametrize("field,value", [
    ("provenance_revision", "0" * 64),
    ("media_probe_signature", "9:9"),
    ("frame_count", 41),
    ("scene_fps", 25.0),
    ("path", "media/other.mp4"),
    ("duration_sec", 2.0),
    ("media_replaced_at", "2026-10-25T00:00:00"),
    ("asset_id", "someone-else"),
])
def test_a_changed_identity_refuses_as_stale(take_project, field, value):
    project, _calls = take_project
    scene = project.get_scene("s1")
    geometry = _geometry([17, 30, 40, 20, 33])
    expected = {**_expected(project, scene, project.get_asset("v1"), geometry), field: value}
    error = _refusal(lambda: _ruler_restore(project, scene, project.get_asset("v1"), geometry,
                                            expected=expected))
    assert (error.status, error.code) == (409, "take_restoration_stale")
    assert error.details["fields"] == [field]


@pytest.mark.parametrize("field", ["source_in_frame", "source_out_frame", "total_source_frames",
                                   "timeline_end_frame"])
def test_a_painted_geometry_the_server_does_not_reach_refuses(take_project, field):
    project, _calls = take_project
    scene = project.get_scene("s1")
    geometry = _geometry([17, 30, 40, 20, 33])
    expected = _expected(project, scene, project.get_asset("v1"), geometry)
    expected[field] += 1
    error = _refusal(lambda: _ruler_restore(project, scene, project.get_asset("v1"), geometry,
                                            expected=expected))
    assert (error.status, error.code) == (409, "take_restoration_stale")


def test_a_cursor_in_the_original_scene_is_not_the_restored_start(take_project):
    project, _calls = take_project
    scene = project.get_scene("s1")
    geometry = _geometry([17, 30, 40, 50, 63])  # a cursor drop the server would not make here
    error = _refusal(lambda: _ruler_restore(project, scene, project.get_asset("v1"), geometry))
    assert error.code == "take_restoration_stale"
    assert "timeline_start_frame" in error.details["fields"]


def test_a_missing_expected_is_a_bad_request(take_project):
    project, _calls = take_project
    scene = project.get_scene("s1")
    op = {"type": "create_clip", "fields": {"asset_id": "v1", "timeline_start_frame": 20,
                                             "restore_take": True}}
    error = _refusal(lambda: routes._apply_scene_mutation_batch(project, "s1", [op]))
    assert (error.status, error.code) == (400, "missing_expected")


@pytest.mark.parametrize("change,reason", [
    (lambda video: setattr(video, "media_replaced_at", "2026-10-25T10:00:00"), "replaced"),
    (lambda video: setattr(video, "trashed_at", "2026-10-25T10:00:00"), "trashed"),
    (lambda video: video.generation_params.pop("take_placement_policy"), "legacy"),
    (lambda video: video.generation_params.update(placement_fps=None), "missing_fps"),
])
def test_an_ineligible_take_refuses_with_its_reason(take_project, change, reason):
    project, _calls = take_project
    scene = project.get_scene("s1")
    video = project.get_asset("v1")
    change(video)
    geometry = _geometry([17, 30, 40, 20, 33])
    error = _refusal(lambda: _ruler_restore(project, scene, video, geometry))
    assert (error.status, error.code) == (409, "take_restoration_unavailable")
    assert error.details["reason"] == reason


def test_missing_media_is_not_restorable(take_project, tmp_path):
    project, _calls = take_project
    (tmp_path / "media" / "take.mp4").unlink()
    scene = project.get_scene("s1")
    error = _refusal(lambda: _ruler_restore(project, scene, project.get_asset("v1"),
                                            _geometry([17, 30, 40, 20, 33])))
    assert error.details["reason"] == "missing"


@pytest.mark.parametrize("family", ["video", "audio"])
def test_an_occupied_range_refuses_the_whole_create(take_project, family):
    project, calls = take_project
    scene = project.get_scene("s1")
    scene.video_lane_count = scene.audio_lane_count = 2
    scene.video_lane_configs = [LaneConfig(), LaneConfig()]
    scene.audio_lane_configs = [LaneConfig(), LaneConfig()]
    if family == "video":
        scene.clips.append(ClipReference(track_index=1, timeline_start_frame=30, timeline_end_frame=40,
                                         source_out_frame=10, total_source_frames=10))
    else:
        scene.audio_tracks.append(AudioTrack(lane_index=1, timeline_start_frame=0, timeline_end_frame=21,
                                             total_source_frames=21))
    geometry = _geometry([17, 30, 40, 20, 33])
    op = {"type": "create_clip",
          "fields": {"asset_id": "v1", "timeline_start_frame": 20, "track_index": 1,
                     "audio_lane_index": 1, "dual_drop": True, "restore_take": True},
          "expected": _expected(project, scene, project.get_asset("v1"), geometry)}
    error = _refusal(lambda: routes._apply_scene_mutation_batch(project, "s1", [op]))
    assert (error.status, error.code) == (409, "lane_collision")
    assert calls["extract"] == 0


def test_a_locked_lane_refuses(take_project):
    project, _calls = take_project
    scene = project.get_scene("s1")
    scene.video_lane_configs = [LaneConfig(locked=True)]
    geometry = _geometry([17, 30, 40, 20, 33])
    op = {"type": "create_clip",
          "fields": {"asset_id": "v1", "timeline_start_frame": 20, "track_index": 0,
                     "restore_take": True},
          "expected": _expected(project, scene, project.get_asset("v1"), geometry)}
    error = _refusal(lambda: routes._apply_scene_mutation_batch(project, "s1", [op]))
    assert error.status == 409


def _no_audio(project, monkeypatch):
    project.assets.remove(project.get_asset("a1"))
    monkeypatch.setattr(routes, "_extract_audio_from_video", lambda *_a, **_k: False)


def test_lost_audio_places_the_video_and_removes_only_the_lane_this_batch_added(take_project, monkeypatch):
    project, _calls = take_project
    _no_audio(project, monkeypatch)
    scene = project.get_scene("s1")
    # A retained config past the lane count, which the batch's count change drops.
    scene.audio_lane_configs = [LaneConfig(name="A1"), LaneConfig(name="retained", hidden=True)]
    before = copy.deepcopy(scene.audio_lane_configs)
    _committed, payload = _ruler_restore(project, scene, project.get_asset("v1"),
                                         _geometry([17, 30, 40, 20, 33]))
    restoration = payload["results"][-1]["take_restoration"]
    assert restoration["audio_source"] == "failed"
    assert restoration["warnings"] == ["Placed the take's video, but its audio could not be restored."]
    assert restoration["removed_audio_lane"] is True
    assert len(scene.clips) == 1 and scene.audio_tracks == []
    assert scene.audio_lane_count == 1 and scene.video_lane_count == 2
    assert scene.audio_lane_configs == before


def test_lost_audio_never_removes_a_lane_that_was_already_there(take_project, monkeypatch):
    project, _calls = take_project
    _no_audio(project, monkeypatch)
    scene = project.get_scene("s1")
    scene.audio_lane_count = 2
    scene.audio_lane_configs = [LaneConfig(), LaneConfig(name="mine")]
    geometry = _geometry([17, 30, 40, 20, 33])
    op = {"type": "create_clip",
          "fields": {"asset_id": "v1", "timeline_start_frame": 20, "track_index": 0,
                     "audio_lane_index": 1, "dual_drop": True, "restore_take": True},
          "expected": _expected(project, scene, project.get_asset("v1"), geometry)}
    _committed, payload = routes._apply_scene_mutation_batch(project, "s1", [op])
    assert payload["results"][0]["take_restoration"]["removed_audio_lane"] is False
    assert scene.audio_lane_count == 2


def test_an_audio_only_restore_that_cannot_recover_audio_fails_visibly(take_project, monkeypatch):
    project, _calls = take_project
    _no_audio(project, monkeypatch)
    scene = project.get_scene("s1")
    geometry = _geometry([17, 30, 40, 20, 33])
    op = {"type": "create_audio_track",
          "fields": {"asset_id": "v1", "timeline_start_frame": 20, "lane_index": 0, "restore_take": True},
          "expected": _expected(project, scene, project.get_asset("v1"), geometry)}
    error = _refusal(lambda: routes._apply_scene_mutation_batch(project, "s1", [op]))
    assert (error.status, error.code) == (409, "take_audio_unavailable")
    assert error.details == {"reason": "no_audio"}
    assert scene.audio_tracks == []


def test_the_take_audio_asset_restores_its_own_trim(take_project):
    project, calls = take_project
    scene = project.get_scene("s2")
    audio = project.get_asset("a1")
    geometry = _geometry([21, 38, 50, 64, 81])
    op = {"type": "create_audio_track",
          "fields": {"asset_id": "a1", "timeline_start_frame": 64, "lane_index": 0, "restore_take": True},
          "expected": _expected(project, scene, audio, geometry)}
    _committed, payload = routes._apply_scene_mutation_batch(project, "s2", [op])
    track = scene.audio_tracks[0]
    assert (track.source_path, track.source_in_frame, track.total_source_frames,
            track.timeline_start_frame, track.timeline_end_frame) == (
        "media/take_audio.wav", 21, 50, 64, 81)
    assert payload["results"][0]["audio_asset"] is None
    assert calls["extract"] == 0


def test_audio_shorter_than_the_range_is_refused(take_project):
    project, _calls = take_project
    scene = project.get_scene("s1")
    audio = project.get_asset("a1")
    audio.duration_sec = 20 / 24
    geometry = _geometry([17, 30, 40, 20, 33])
    op = {"type": "create_audio_track",
          "fields": {"asset_id": "a1", "timeline_start_frame": 20, "lane_index": 0, "restore_take": True},
          "expected": _expected(project, scene, audio, geometry)}
    error = _refusal(lambda: routes._apply_scene_mutation_batch(project, "s1", [op]))
    assert error.details["reason"] == "audio_short"


@pytest.mark.parametrize("spoil", [
    lambda audio: audio.generation_params.update(source_video_asset_id="someone-else"),
    lambda audio: setattr(audio, "trashed_at", "2026-10-25T00:00:00"),
    lambda audio: setattr(audio, "media_replaced_at", "2026-10-25T00:00:00"),
    lambda audio: audio.generation_params.update(encoded_frame_count=39),
    lambda audio: audio.generation_params.pop("take_placement_policy"),
])
def test_only_a_verified_sibling_is_the_takes_audio(take_project, spoil):
    project, calls = take_project
    spoil(project.get_asset("a1"))
    scene = project.get_scene("s1")
    _committed, payload = _ruler_restore(project, scene, project.get_asset("v1"),
                                         _geometry([17, 30, 40, 20, 33]))
    assert payload["results"][-1]["take_restoration"]["audio_source"] == "extracted"
    assert scene.audio_tracks[0].source_path != "media/take_audio.wav"
    assert scene.audio_tracks[0].source_in_frame == 17
    assert calls["extract"] == 1


def test_a_bound_extraction_never_reuses_an_unproven_sidecar(take_project, tmp_path):
    project, calls = take_project
    video = project.get_asset("v1")
    (tmp_path / "media" / "v1_audio.wav").write_bytes(b"stale" * 1000)
    stale = Asset(asset_id="old", name="old", asset_type="audio", path="media/v1_audio.wav",
                  duration_sec=1.0)
    project.assets.append(stale)
    # The ordinary drop keeps its old reuse of the shared path.
    assert routes._prepare_video_audio_asset(project, video) is stale
    assert calls["extract"] == 0
    bound = routes._prepare_video_audio_asset(project, video, bind_identity=True)
    assert bound is not stale and bound.path.replace("\\", "/").startswith("media/v1_audio_")
    assert (tmp_path / "media" / "v1_audio.wav").read_bytes() == b"stale" * 1000
    assert routes._prepare_video_audio_asset(project, video, bind_identity=True) is bound
    assert calls["extract"] == 1
    # A probe signature filled in later is not a new identity...
    video.media_probe_signature = "2000:2"
    assert routes._prepare_video_audio_asset(project, video, bind_identity=True) is bound
    # ...a different length is.
    video.frame_count = 41
    assert routes._prepare_video_audio_asset(project, video, bind_identity=True) is not bound
    assert calls["extract"] == 2


def test_extracted_sidecars_carry_no_provenance(take_project):
    """Output Views reads `has_provenance` on audio as "a take's own audio"."""
    project, _calls = take_project
    video = project.get_asset("v1")
    for bind in (False, True):
        derived = routes._prepare_video_audio_asset(project, video, bind_identity=bind)
        assert derived.generation_params == {}
        assert routes._asset_payload(project, derived, lean=True)["has_provenance"] is False


def test_an_unusable_extraction_leaves_no_asset_or_file(take_project, tmp_path, monkeypatch):
    project, _calls = take_project
    project.assets.remove(project.get_asset("a1"))
    # Shorter than the restored source start: extracted, then rejected.
    monkeypatch.setattr(routes, "_get_audio_duration", lambda *_a, **_k: 10 / 24)
    scene = project.get_scene("s1")
    before = {asset.asset_id for asset in project.assets}
    _committed, payload = _ruler_restore(project, scene, project.get_asset("v1"),
                                         _geometry([17, 30, 40, 20, 33]))
    assert payload["results"][-1]["take_restoration"]["audio_source"] == "failed"
    assert {asset.asset_id for asset in project.assets} == before
    assert sorted(path.name for path in (tmp_path / "media").iterdir()) == ["take.mp4", "take_audio.wav"]


def test_a_dual_restore_without_embedded_audio_still_uses_the_takes_audio(take_project):
    project, calls = take_project
    project.get_asset("v1").has_audio = False
    scene = project.get_scene("s1")
    _committed, payload = _ruler_restore(project, scene, project.get_asset("v1"),
                                         _geometry([17, 30, 40, 20, 33]))
    assert payload["results"][-1]["take_restoration"]["audio_source"] == "original"
    assert calls["extract"] == 0


def test_a_dual_restore_with_no_audio_at_all_removes_its_lane_and_warns(take_project):
    project, _calls = take_project
    project.assets.remove(project.get_asset("a1"))
    project.get_asset("v1").has_audio = False
    scene = project.get_scene("s1")
    _committed, payload = _ruler_restore(project, scene, project.get_asset("v1"),
                                         _geometry([17, 30, 40, 20, 33]))
    restoration = payload["results"][-1]["take_restoration"]
    assert restoration["audio_source"] == "failed" and restoration["warnings"]
    assert restoration["removed_audio_lane"] is True and scene.audio_lane_count == 1


def _sibling(project, tmp_path, asset_id, path, video_id, **params):
    (tmp_path / path).write_bytes(b"audio" * 400)
    project.assets.append(Asset(
        asset_id=asset_id, name=asset_id, asset_type="audio", path=path, duration_sec=40 / 24,
        generation_params={**copy.deepcopy(BASE), "encoded_frame_count": 40,
                           "source_video_asset_id": video_id, **params}))


def test_the_newest_takes_audio_is_found_past_many_older_retakes(take_project, tmp_path):
    project, calls = take_project
    project.assets.remove(project.get_asset("a1"))
    for index in range(20):  # earlier retakes of the same selection, each with its own audio
        _sibling(project, tmp_path, f"old{index}", f"media/old{index}_audio.wav", f"oldvideo{index}")
    _sibling(project, tmp_path, "mine", "media/unrelated_name.wav", "v1")
    scene = project.get_scene("s1")
    _ruler_restore(project, scene, project.get_asset("v1"), _geometry([17, 30, 40, 20, 33]))
    assert scene.audio_tracks[0].source_path == "media/unrelated_name.wav"
    assert calls["extract"] == 0


@pytest.mark.parametrize("name", ["take_audio.wav", "v1_audio.wav"])
def test_a_producer_named_sibling_is_found_without_a_shared_selection(take_project, tmp_path, name):
    """Save Video names a take's audio `{video stem}_audio.wav`; export `{asset_id}_audio.wav`."""
    project, calls = take_project
    project.assets.remove(project.get_asset("a1"))
    # The oldest of 21 same-selection candidates: only its name brings it inside the cap.
    _sibling(project, tmp_path, "mine", f"media/{name}", "v1")
    for index in range(20):
        _sibling(project, tmp_path, f"old{index}", f"media/old{index}_audio.wav", f"oldvideo{index}")
    scene = project.get_scene("s1")
    _ruler_restore(project, scene, project.get_asset("v1"), _geometry([17, 30, 40, 20, 33]))
    assert scene.audio_tracks[0].source_path == f"media/{name}"
    assert calls["extract"] == 0


def test_an_export_takes_audio_is_its_sibling(take_project, tmp_path):
    project, calls = take_project
    project.assets.remove(project.get_asset("a1"))
    export = {"take_placement_policy": "authored_v1", "take_keep_padding": False, "placement_fps": 24,
              "editor_export": {"scene_id": "s1", "range": {"start": 50, "end": 80}, "fps": 24}}
    video = project.get_asset("v1")
    video.generation_params = copy.deepcopy(export)
    video.frame_count, video.duration_sec = 30, 30 / 24
    (tmp_path / "media" / "v1_audio.wav").write_bytes(b"audio" * 400)
    project.assets.append(Asset(asset_id="xa", name="export audio", asset_type="audio",
                                path="media/v1_audio.wav", duration_sec=30 / 24,
                                generation_params={**copy.deepcopy(export), "encoded_frame_count": 30,
                                                   "source_video_asset_id": "v1"}))
    scene = project.get_scene("s1")
    _committed, payload = _ruler_restore(project, scene, video, _geometry([0, 30, 30, 50, 80]))
    assert payload["results"][-1]["take_restoration"]["audio_source"] == "original"
    assert scene.audio_tracks[0].source_path == "media/v1_audio.wav"
    assert calls["extract"] == 0


def test_media_changed_on_disk_to_another_length_ends_restoration(take_project, tmp_path, monkeypatch):
    project, _calls = take_project
    video = project.get_asset("v1")
    video.width = video.height = 8
    video.has_audio_checked = True
    probed = {"width": 8, "height": 8, "fps": 24.0, "sample_rate": 0, "has_audio": True}
    monkeypatch.setattr(routes, "_extract_asset_media_metadata",
                        lambda *_a, **_k: {**probed, "frame_count": 44, "duration_sec": 44 / 24})
    monkeypatch.setattr(routes, "probe_video_color_metadata", lambda *_a, **_k: {})
    routes._sync_media_folder(project, purge_trashed=False)
    assert video.frame_count == 44 and video.media_replaced_at

    # One frame of probe disagreement on a touched file is not a replacement.
    video.media_replaced_at, video.frame_count, video.media_probe_signature = "", 40, "2000:1"
    monkeypatch.setattr(routes, "_extract_asset_media_metadata",
                        lambda *_a, **_k: {**probed, "frame_count": 41, "duration_sec": 41 / 24})
    routes._sync_media_folder(project, purge_trashed=False)
    assert video.media_replaced_at == ""


def test_replacing_media_marks_the_asset_and_keeps_its_provenance(take_project, tmp_path, monkeypatch):
    project, _calls = take_project
    video = project.get_asset("v1")
    replacement = tmp_path / "replacement.mp4"
    replacement.write_bytes(b"new video")
    monkeypatch.setattr(routes, "_resolve_source_path", lambda path: path)
    monkeypatch.setattr(routes, "_classify_asset_for_registration", lambda _path: ("video", ""))
    monkeypatch.setattr(routes, "_extract_asset_media_metadata", lambda *_a, **_k: {
        "width": 8, "height": 8, "frame_count": 40, "fps": 24.0, "duration_sec": 40 / 24,
        "sample_rate": 0, "has_audio": True})
    routes._replace_project_asset(project, video, str(replacement))
    assert video.media_replaced_at
    assert video.generation_params["take_placement_policy"] == "authored_v1"
    assert take_restoration_source(video.generation_params, "video")["status"] == "supported"
    assert Asset.from_dict(video.to_dict()).media_replaced_at == video.media_replaced_at


def test_lazy_storage_hydrates_only_the_dropped_take_and_its_audio(tmp_path, monkeypatch):
    project = pm.create_project("Lazy", base_dir=str(tmp_path))
    media = Path(project.project_dir) / "media"
    media.mkdir(exist_ok=True)
    (media / "take.mp4").write_bytes(b"video" * 400)
    (media / "take_audio.wav").write_bytes(b"audio" * 400)
    scene = Scene(name="Lazy", fps=24.0, duration_frames=96)
    project.scenes.append(scene)
    params = {**copy.deepcopy(BASE), "scene_id": scene.scene_id}
    project.assets = [
        Asset(asset_id="v1", name="take.mp4", asset_type="video", path="media/take.mp4",
              frame_count=40, fps=24.0, duration_sec=40 / 24, has_audio=True, generation_params=params),
        Asset(asset_id="a1", name="take audio", asset_type="audio", path="media/take_audio.wav",
              duration_sec=40 / 24, generation_params={**params, "encoded_frame_count": 40,
                                                       "source_video_asset_id": "v1"}),
        *[Asset(asset_id=f"other{i}", asset_type="audio", path=f"media/other{i}.wav",
                duration_sec=1.0, generation_params={"prompt": "unrelated", "scene_id": "elsewhere"})
          for i in range(20)],
    ]
    pm.save_project(project, notify=False)
    loaded = pm.load_project(project.project_dir)
    scene = loaded.scenes[0]
    original = ps.hydrate_asset
    hydrated = []

    def counted(asset):
        if getattr(asset, "_generation_unhydrated", False):
            hydrated.append(asset.asset_id)
        return original(asset)

    monkeypatch.setattr(ps, "hydrate_asset", counted)
    monkeypatch.setattr(routes, "ensure_thumbnail", lambda *a, **k: None)
    video = loaded.get_asset("v1")
    expected = _expected(loaded, scene, video, _geometry([17, 30, 40, 20, 33]))
    assert hydrated == []
    _ruler_restore(loaded, scene, video, _geometry([17, 30, 40, 20, 33]), expected=expected)
    assert sorted(hydrated) == ["a1", "v1"]
    assert scene.audio_tracks[0].source_path == "media/take_audio.wav"


def test_an_ordinary_drop_is_unchanged_by_a_takes_record(take_project):
    """Restoration is opt-in per request: without it a take drops whole at the cursor."""
    project, _calls = take_project
    scene = project.get_scene("s1")
    op = {"type": "create_clip", "fields": {"asset_id": "v1", "timeline_start_frame": 5, "track_index": 0}}
    _committed, payload = routes._apply_scene_mutation_batch(project, "s1", [op])
    clip = scene.clips[0]
    assert (clip.timeline_start_frame, clip.source_in_frame, clip.source_out_frame) == (5, 0, 40)
    assert "take_restoration" not in payload["results"][0]
