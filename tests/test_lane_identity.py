"""Media lanes have ids of their own, and scene history merges lane by lane (step 2b).

The first half is the identity: given at load, minted once for a lane created since,
and kept by every writer. The second half runs real operations, real take placement,
the real merge and the real post-merge validation through a matrix of steps and
concurrent changes, judged by an oracle that knows which lane is which.
"""
import asyncio
import copy
import itertools
import json

import pytest

import server.routes as routes
from server import project_commit, project_manager
from server.lane_registry import (LANE_DESCRIPTORS_BY_LANE_TYPE, allocate_take_lane)
from server.scene_history_merge import SceneMergeConflict, merge_scene_history
from server.timeline_state import (AudioTrack, ClipReference, LaneConfig, ReferenceItem,
                                   ReferenceLaneRecipe, Scene, TimelineProject)
from test_history_validation import restore_case
from test_phase43_routes import (DummyRequest, _load_route_module, _response_json, _route_handler)

MEDIA = ("video", "motion_driver", "audio")
media = pytest.mark.parametrize("family", MEDIA)
BLANK = {"name": "", "color": "", "locked": False, "hidden": False}


def _wire(value):
    return json.loads(json.dumps(value))


def _ids(scene, family="video"):
    scene = scene if isinstance(scene, dict) else scene.to_dict()
    return [row.get("lane_id", "") for row in scene[f"{family}_lane_configs"]]


def _legacy_scene(family="video", count=3, configs=None, **values):
    """A scene 0.9.3 saved: lanes with no ids."""
    return {"scene_id": "scene-1", "duration_frames": 200, f"{family}_lane_count": count,
            f"{family}_lane_configs": configs if configs is not None
            else [dict(BLANK) for _ in range(count)], **values}


def _batch(scene, *operations):
    project = TimelineProject(project_id="project", scenes=[scene])
    return routes._apply_scene_mutation_batch(project, scene.scene_id, list(operations))


# --------------------------------------------------------------------------
# Identity
# --------------------------------------------------------------------------

@media
@pytest.mark.parametrize("configs", [None, [], [dict(BLANK)]])
def test_a_lane_saved_without_an_id_gets_the_same_one_on_every_load(family, configs):
    raw = _legacy_scene(family, configs=configs)
    first, second = Scene.from_dict(raw), Scene.from_dict(copy.deepcopy(raw))
    ids = _ids(first, family)
    assert len(set(ids)) == 3 and all(ids)
    assert ids == _ids(second, family)
    others = {value for other in MEDIA if other != family for value in _ids(first, other)}
    assert not others & set(ids)
    # Another scene's lanes are not these lanes.
    assert not set(ids) & set(_ids(Scene.from_dict({**raw, "scene_id": "other"}), family))


def test_a_stored_id_is_kept_and_a_repeated_one_is_not_shared():
    raw = _legacy_scene(configs=[{**BLANK, "lane_id": "a"}, {**BLANK, "lane_id": "a"},
                                 {**BLANK, "lane_id": "c"}])
    ids = _ids(Scene.from_dict(raw))
    assert ids[0] == "a" and ids[2] == "c" and len(set(ids)) == 3
    assert ids == _ids(Scene.from_dict(raw))


def test_loading_writes_nothing():
    raw = _legacy_scene()
    before = copy.deepcopy(raw)
    Scene.from_dict(raw)
    assert raw == before


def test_a_write_that_changes_nothing_is_not_a_write_on_an_older_project():
    changed, _ = _batch(Scene.from_dict(_legacy_scene()), {
        "type": "update_lane_config", "lane_type": "video", "lane_index": 0,
        "fields": {"locked": False}})
    assert changed is False


def test_only_media_lanes_are_identified_here():
    raw = {"scene_id": "s", "guide_track_config": {"name": "g"},
           "prompt_track_config": {"lane_id": "future", "name": "p"},
           "reference_lane_count": 2,
           "reference_lane_configs": [{"lane_id": "odd", "name": "r"}, {}]}
    scene = Scene.from_dict(raw)
    saved = scene.to_dict()
    assert "lane_id" not in saved["guide_track_config"]
    assert "lane_id" not in saved["reference_lane_configs"][1]
    # Unknown there, so kept at rest like any other unknown field, and nobody's identity.
    assert saved["prompt_track_config"]["lane_id"] == "future"
    assert saved["reference_lane_configs"][0]["lane_id"] == "odd"
    assert all(config.lane_id == "" for config in scene.reference_lane_configs)
    assert LaneConfig().to_dict() == BLANK


def test_a_repeated_reference_id_is_repaired_at_load_and_never_written():
    """Two lanes with one id refused every step on the family, and no reload helped."""
    raw = {"scene_id": "scene-1", "duration_frames": 50, "reference_lane_count": 3,
           "reference_lane_configs": [{}, {}, {}],
           "reference_lane_recipes": [{"lane_id": "a", "recipe_id": str(i)} for i in range(3)]}
    ids = [recipe.lane_id for recipe in Scene.from_dict(raw).reference_lane_recipes]
    assert ids[0] == "a" and len(set(ids)) == 3 and all(ids)
    assert ids == [recipe.lane_id for recipe in Scene.from_dict(raw).reference_lane_recipes]
    scene = Scene.from_dict(raw)
    _, payload = _batch(scene, {"type": "update_lane_configs", "fields": {
        "reference_lane_recipes": [{"lane_id": "z"}, {"lane_id": "z"}, {"lane_id": "z"}]}})
    ids = [row["lane_id"] for row in payload["scene"]["reference_lane_recipes"]]
    assert ids[0] == "z" and len(set(ids)) == 3


@pytest.mark.parametrize("fields", [
    {"video_lane_configs": [{**BLANK, "evil": 1, "lane_id": "client-lane-1"}] * 2},
    {"guide_track_config": {**BLANK, "name": "g", "evil": 1}},
    {"prompt_track_config": {**BLANK, "evil": 1}},
    {"reference_lane_configs": [{**BLANK, "evil": 1}]},
])
def test_a_request_cannot_store_fields_of_its_own(fields, monkeypatch, tmp_path):
    raw = _legacy_scene(count=1, configs=[], guide_track_config={"name": "old", "future": 1})
    scene = Scene.from_dict(raw)
    _, payload = _batch(scene, {"type": "update_lane_configs", "fields": fields})
    assert "evil" not in json.dumps(payload["scene"]) and "client-lane-1" not in json.dumps(payload)
    assert payload["scene"]["guide_track_config"]["future"] == 1
    module = _load_route_module(monkeypatch)
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[Scene.from_dict(raw)])
    monkeypatch.setattr(module, "_load_project_from_request", lambda request, **kw: project)
    monkeypatch.setattr(module, "save_project", lambda project, **kw: None)
    handler = _route_handler(module, "PUT", "/sonder-editor/project/{project_id}/scenes/{scene_id}")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"}, body=dict(fields))))
    assert response.status == 200 and "evil" not in json.dumps(project.scenes[0].to_dict())


@pytest.mark.parametrize("rows", [None, {}, "x", 5, [None, 3, "x"]])
def test_a_malformed_whole_array_keeps_the_lanes(rows):
    scene = Scene.from_dict(_legacy_scene())
    ids = _ids(scene)
    _batch(scene, {"type": "update_lane_configs", "fields": {"video_lane_configs": rows}})
    assert _ids(scene) == ids


def test_a_drop_that_takes_its_audio_lane_back_leaves_the_lanes_as_they_were():
    raw = _legacy_scene("audio", 1, [{**BLANK, "lane_id": "a0"}, {**BLANK, "lane_id": "kept"}])
    scene = Scene.from_dict(raw)
    start = {"audio_lane_count": 1, "audio_lane_configs": copy.deepcopy(scene.audio_lane_configs)}
    routes._set_scene_lane_count(scene, "audio", 2)
    assert routes._remove_batch_created_audio_lane(scene, 1, start)
    scene.identify_new_lanes()
    assert scene.audio_lane_count == 1 and _ids(scene, "audio") == ["a0", "kept"]


@media
def test_a_new_lane_never_takes_a_removed_lanes_id(family):
    """The alias: an id derived from the position would return with the position."""
    scene = Scene.from_dict(_legacy_scene(family))
    removed = _ids(scene, family)[2]
    _batch(scene, {"type": "remove_lane", "lane_type": family, "lane_index": 2,
                   "item_policy": "require_empty",
                   "expected": {"lane_count": 3, "config": BLANK}})
    kept = _ids(scene, family)
    scene = Scene.from_dict(_wire(scene.to_dict()))
    _, payload = _batch(scene, {"type": "set_lane_count", "lane_type": family, "count": 3})
    ids = _ids(payload["scene"], family)
    assert ids[:2] == kept and ids[2] and ids[2] != removed
    assert ids == _ids(Scene.from_dict(_wire(payload["scene"])), family)


def test_a_batch_answers_and_describes_the_ids_it_saves():
    scene = Scene.from_dict(_legacy_scene())
    _, payload = _batch(scene, {"type": "set_lane_count", "lane_type": "audio", "count": 2})
    assert payload["scene"] == scene.to_dict()
    assert _ids(scene, "audio")[1] == _ids(payload["scene"], "audio")[1] != ""


@pytest.mark.parametrize(("offered", "kept"), [
    (["client-lane-1", "client-lane-2"], [True, True]),
    (["client-lane-1", "client-lane-1"], [True, False]),      # repeated
    (["short", "client-lane-2"], [False, True]),              # malformed
    (["EXISTING", None], [False, False]),                     # in use, not text
    ("client-lane-1", [False, False]), (None, [False, False]), ([], [False, False]),
    (["client-lane-1", "client-lane-2", "client-lane-3"], [True, True]),
])
def test_the_editor_may_name_the_lanes_it_adds(offered, kept):
    scene = Scene.from_dict(_legacy_scene(count=1))
    existing = _ids(scene)[0]
    if isinstance(offered, list):
        offered = [existing if value == "EXISTING" else value for value in offered]
    _, payload = _batch(scene, {"type": "set_lane_count", "lane_type": "video", "count": 3,
                                "lane_ids": offered})
    ids = _ids(payload["scene"])
    assert ids[0] == existing and len(set(ids)) == 3 and all(ids)
    for index, used in enumerate(kept):
        assert (isinstance(offered, list) and index < len(offered)
                and ids[index + 1] == offered[index]) is used


def test_a_reference_lanes_identity_stays_its_recipes():
    scene = Scene.from_dict(_legacy_scene())
    _, payload = _batch(scene, {"type": "set_lane_count", "lane_type": "reference", "count": 2,
                                "lane_ids": ["client-lane-1"]})
    assert "lane_id" not in payload["scene"]["reference_lane_configs"][1]
    assert payload["scene"]["reference_lane_recipes"][1]["lane_id"] not in ("", "client-lane-1")


def test_configs_past_the_count_keep_their_ids_when_a_take_adopts_them():
    raw = _legacy_scene(count=1, configs=[dict(BLANK), dict(BLANK), dict(BLANK)], clips=[
        ClipReference(clip_id="c", track_index=0, timeline_end_frame=8).to_dict()])
    scene = Scene.from_dict(raw)
    ids = _ids(scene)
    assert allocate_take_lane(scene, "video", LaneConfig) == 1
    scene.identify_new_lanes()
    assert _ids(scene) == ids and scene.video_lane_count == 2


@pytest.mark.parametrize("writer", ["operation", "put"])
def test_a_whole_array_keeps_identity_and_unknown_fields(writer, monkeypatch, tmp_path):
    raw = _legacy_scene(configs=[{**BLANK, "lane_id": f"id{index}", "future": index}
                                 for index in range(3)])
    rows = [{**BLANK, "name": "new", "lane_id": "id2"}, {**BLANK, "lane_id": "id2"}]
    scene = Scene.from_dict(raw)
    if writer == "operation":
        _batch(scene, {"type": "update_lane_configs", "fields": {"video_lane_configs": rows}})
    else:
        module = _load_route_module(monkeypatch)
        project = TimelineProject(project_id="project", project_dir=str(tmp_path), scenes=[scene])
        monkeypatch.setattr(module, "_load_project_from_request", lambda request, **kw: project)
        monkeypatch.setattr(module, "save_project", lambda project, **kw: None)
        handler = _route_handler(module, "PUT", "/sonder-editor/project/{project_id}/scenes/{scene_id}")
        response = asyncio.run(handler(DummyRequest(
            match_info={"project_id": "project", "scene_id": "scene-1"},
            body={"video_lane_configs": rows})))
        assert response.status == 200, _response_json(response)
    saved = scene.to_dict()["video_lane_configs"]
    assert [row["lane_id"] for row in saved] == ["id0", "id1", "id2"]
    assert [row["future"] for row in saved] == [0, 1, 2]
    assert saved[0]["name"] == "new"


@pytest.mark.parametrize("family", [*MEDIA, "reference"])
def test_unknown_fields_go_with_their_lane(family):
    """They used to be put back by position: a removed lane's neighbour got them."""
    raw = _legacy_scene(family, configs=[{**BLANK, "future": name} for name in "abc"])
    if family == "reference":
        raw["reference_lane_recipes"] = [{"lane_id": f"r{index}"} for index in range(3)]
    scene = Scene.from_dict(raw)
    expected = {"lane_count": 3, "config": BLANK}
    if family == "reference":
        expected["lane_id"] = "r0"
    _, payload = _batch(scene, {"type": "remove_lane", "lane_type": family, "lane_index": 0,
                                "item_policy": "require_empty", "expected": expected})
    def futures(value):
        return [row.get("future") for row in value[f"{family}_lane_configs"]]
    assert futures(payload["scene"]) == ["b", "c"]
    assert futures(Scene.from_dict(_wire(payload["scene"])).to_dict()) == ["b", "c"]
    if family == "reference":
        _, payload = _batch(scene, {
            "type": "move_lane", "lane_type": "reference", "from_index": 0, "to_index": 1,
            "expected": {"from_lane_id": "r1", "to_lane_id": "r2"}})
        assert futures(payload["scene"]) == ["c", "b"]


def test_every_saved_project_gives_its_new_lanes_ids(tmp_path):
    project = project_manager.create_project("ZZ-lane-ids", base_dir=str(tmp_path))
    if not project.scenes:
        project.add_scene(Scene(scene_id="scene-1", name="Scene"))
    scene = project.scenes[0]
    allocate_take_lane(scene, "video", LaneConfig)
    scene.video_lane_count = 3                     # a writer that only raised the count
    project_manager.save_project(project)
    ids = _ids(scene)
    assert len(ids) == 3 and all(ids) and len(set(ids)) == 3
    assert _ids(project_manager.load_project(project.project_dir).scenes[0]) == ids
    # Saved once, they are not minted again.
    project_manager.save_project(project)
    assert _ids(scene) == ids


def test_a_take_on_the_conflict_path_brings_a_lane_of_its_own():
    current = TimelineProject(scenes=[Scene.from_dict(_legacy_scene(count=1, clips=[
        ClipReference(clip_id="c", track_index=0, timeline_end_frame=8).to_dict()]))])
    produced = copy.deepcopy(current)
    take = ClipReference(clip_id="take", track_index=1, timeline_end_frame=8, is_generated=True)
    produced.scenes[0].clips.append(take)
    before = _ids(current.scenes[0])
    assert project_commit._merge_generated_outputs(
        current, produced, {"clips": {"take"}, "assets": set(), "audio": set(), "links": set()},
        auto_placement_ids={"clips": {"take"}})
    current.scenes[0].identify_new_lanes()
    ids = _ids(current.scenes[0])
    assert ids[0] == before[0] and len(set(ids)) == 2


@media
@pytest.mark.parametrize(("sent", "status"), [("own", None), ("other", 409), ("", None)])
def test_removing_a_lane_names_it_by_id(family, sent, status):
    """Two default lanes are one lane to the count and the settings."""
    scene = Scene.from_dict(_legacy_scene(family))
    stale = _ids(scene, family)[1]
    expected = {"lane_count": 3, "config": BLANK}
    if sent == "other":
        # Another editor removed lane 0 and added one: index 1 is another lane now.
        _batch(scene, {"type": "remove_lane", "lane_type": family, "lane_index": 0,
                       "item_policy": "require_empty", "expected": dict(expected)})
        _batch(scene, {"type": "set_lane_count", "lane_type": family, "count": 3})
    if sent:
        expected["lane_id"] = stale
    remove = {"type": "remove_lane", "lane_type": family, "lane_index": 1,
              "item_policy": "require_empty", "expected": expected}
    if status is None:
        _batch(scene, remove)
        assert stale not in _ids(scene, family)
    else:
        before = _ids(scene, family)
        with pytest.raises(routes.ProjectMutationRequestError) as exc:
            _batch(scene, remove)
        assert (exc.value.status, exc.value.code) == (409, "identity_mismatch")
        assert _ids(scene, family) == before


@media
@pytest.mark.parametrize(("sent", "refused"), [
    ("own", False), ("other", True), ("", False), (None, False), ("absent", False)])
def test_a_lane_setting_names_its_lane_by_id(family, sent, refused):
    """A lock, rename, hide or colour written by position can land on the lane that took
    the position: here another page removed lane 0 and added one."""
    scene = Scene.from_dict(_legacy_scene(family))
    stale = _ids(scene, family)[1]
    _batch(scene, {"type": "remove_lane", "lane_type": family, "lane_index": 0,
                   "item_policy": "require_empty",
                   "expected": {"lane_count": 3, "config": BLANK}})
    _batch(scene, {"type": "set_lane_count", "lane_type": family, "count": 3})
    write = {"type": "update_lane_config", "lane_type": family, "lane_index": 1,
             "fields": {"name": "N", "locked": True, "hidden": True, "color": "#fff"},
             "expected": {"lane_id": _ids(scene, family)[1] if sent == "own" else
                          stale if sent == "other" else sent}}
    if sent == "absent":                       # a page older than lane ids
        del write["expected"]
    before = scene.to_dict()
    if refused:
        with pytest.raises(routes.ProjectMutationRequestError) as exc:
            _batch(scene, write)
        assert (exc.value.status, exc.value.code) == (409, "identity_mismatch")
        assert scene.to_dict() == before, "a refusal writes nothing"
    else:
        _batch(scene, write)
        saved = scene.to_dict()[f"{family}_lane_configs"][1]
        assert saved["name"] == "N" and saved["lane_id"] == _ids(before, family)[1]


@pytest.mark.parametrize("operation", ["remove_lane", "update_lane_config"])
@pytest.mark.parametrize("value", [{"a": 1}, ["x"], 5, True, "x" * 10_000, 0, False, [], {}])
def test_a_malformed_lane_id_is_refused_not_a_crash(operation, value):
    scene = Scene.from_dict(_legacy_scene())
    with pytest.raises(routes.ProjectMutationRequestError) as exc:
        _batch(scene, {"type": operation, "lane_type": "video", "lane_index": 1, "fields": {},
                       "item_policy": "require_empty",
                       "expected": {"lane_count": 3, "config": BLANK, "lane_id": value}})
    assert (exc.value.status, exc.value.code) == (409, "identity_mismatch")


class _Race:
    """One write through the real commit loop, another writer saving in between."""

    def __init__(self, monkeypatch, tmp_path, family, count=2):
        self.module = _load_route_module(monkeypatch)
        self.project = project_manager.create_project("ZZ-lane-race", base_dir=str(tmp_path))
        scene = Scene.from_dict(_legacy_scene(family, count=count))
        scene.clips.append(ClipReference(clip_id="mine", timeline_end_frame=8, muted=True))
        self.project.scenes = [scene]
        project_manager.save_project(self.project)
        self.ids = _ids(scene, family)
        self.family, self.other, self.raced = family, None, 0
        real_save = self.module.save_project

        def racing_save(current, **kwargs):
            if not self.raced:
                self.raced += 1
                other = project_manager.load_project(self.project.project_dir)
                self.other(other.scenes[0])
                project_manager.save_project(other)
            return real_save(current, **kwargs)

        monkeypatch.setattr(self.module, "save_project", racing_save)
        monkeypatch.setattr(self.module, "_load_project_from_request",
                            lambda request, **kw: project_manager.load_project(self.project.project_dir))

    def write(self, *operations, other):
        self.other = other
        request = DummyRequest(match_info={"project_id": "project", "scene_id": "scene-1"})
        _, payload = self.module._apply_scene_mutations_sync(request, "scene-1", list(operations))
        return payload

    def lane(self, **fields):
        return {"type": "update_lane_config", "lane_type": self.family, "lane_index": 1,
                "fields": fields, "expected": {"lane_id": self.ids[1]}}

    @property
    def scene(self):
        assert self.raced
        return project_manager.load_project(self.project.project_dir).scenes[0].to_dict()


def _take_lands(scene):
    scene.clips.append(ClipReference(clip_id="take", timeline_end_frame=8))


@media
@pytest.mark.parametrize("named", [True, False])
def test_a_lane_setting_survives_a_save_race_only_when_it_names_its_lane(
        family, named, monkeypatch, tmp_path):
    """A take lands between this write's load and its save. Named, the write is applied
    to the document that holds the take; by position it is refused, as before."""
    race = _Race(monkeypatch, tmp_path, family)
    operation = race.lane(locked=True)
    if named:
        payload = race.write(operation, other=_take_lands)
        # The step's before-state is the attempt that committed: the take is in both.
        assert "clips" not in payload["history_before"]["members"]
        assert payload["history_before"]["fields"].keys() == {f"{family}_lane_configs"}
    else:
        del operation["expected"]
        with pytest.raises(project_manager.ProjectVersionConflict):
            race.write(operation, other=_take_lands)
    assert [clip["clip_id"] for clip in race.scene["clips"]] == ["mine", "take"]
    assert race.scene[f"{family}_lane_configs"][1]["locked"] is named


@media
def test_a_raced_lane_setting_keeps_another_writers_setting_on_that_lane(
        family, monkeypatch, tmp_path):
    """The editor sends only what its gesture changed, so the retry puts nothing back."""
    race = _Race(monkeypatch, tmp_path, family)
    def lock(scene):
        getattr(scene, f"{family}_lane_configs")[1].locked = True
    race.write(race.lane(name="Mine"), other=lock)
    row = race.scene[f"{family}_lane_configs"][1]
    assert (row["name"], row["locked"]) == ("Mine", True)


@media
@pytest.mark.parametrize("removed", [0, 1])
def test_a_raced_lane_setting_never_lands_on_the_lane_that_took_its_number(
        family, removed, monkeypatch, tmp_path):
    race = _Race(monkeypatch, tmp_path, family, count=3)
    def remove(scene):
        getattr(scene, f"{family}_lane_configs").pop(removed)
        setattr(scene, f"{family}_lane_count", 2)
    with pytest.raises(race.module.ProjectMutationRequestError) as exc:
        race.write(race.lane(name="Mine"), other=remove)
    assert (exc.value.status, exc.value.code) == (409, "identity_mismatch")
    assert [row["name"] for row in race.scene[f"{family}_lane_configs"]] == ["", ""]


@media
def test_a_named_lane_past_the_last_one_is_refused_as_moved(family):
    scene = Scene.from_dict(_legacy_scene(family, count=1))
    write = {"type": "update_lane_config", "lane_type": family, "lane_index": 4,
             "fields": {"name": "x"}}
    for expected, code in (({"lane_id": "gone-lane"}, "identity_mismatch"), (None, "item_not_found")):
        with pytest.raises(routes.ProjectMutationRequestError) as exc:
            _batch(scene, {**write, **({"expected": expected} if expected else {})})
        assert exc.value.code == code


def test_a_raced_visibility_toggle_is_applied_whole(monkeypatch, tmp_path):
    """A header toggle sends its lane with the items it unmutes: all or nothing."""
    race = _Race(monkeypatch, tmp_path, "video")
    race.write(race.lane(hidden=False),
               {"type": "update_clip", "clip_id": "mine", "fields": {"muted": False}},
               other=_take_lands)
    assert [(clip["clip_id"], clip["muted"]) for clip in race.scene["clips"]] == [
        ("mine", False), ("take", False)]


@pytest.mark.parametrize(("wanted", "stale"), [
    ({}, False), ({"lane_id": "ID"}, False), ({"lane_id": "gone"}, True),
    ({"name": "other"}, True), (None, False), ({"future": 1}, False)])
def test_a_take_drop_compares_lanes_not_every_stored_field(wanted, stale):
    scene = Scene.from_dict(_legacy_scene(count=1, configs=[{**BLANK, "unknown": 1}]))
    if isinstance(wanted, dict):
        wanted = {**BLANK, **wanted}
        if wanted.get("lane_id") == "ID":
            wanted["lane_id"] = _ids(scene)[0]
    def check():
        routes._validate_take_restoration_lanes(
            scene, {"lane_baseline": {"video_lane_count": 1, "video_lane_config": wanted}},
            None, [("video", 0)])
    if stale:
        with pytest.raises(routes.ProjectMutationRequestError):
            check()
    else:
        check()


def test_a_created_scene_answers_with_the_ids_it_saved(monkeypatch, tmp_path):
    module = _load_route_module(monkeypatch)
    project = project_manager.create_project("ZZ-lane-scene", base_dir=str(tmp_path))
    monkeypatch.setattr(module, "_load_project_from_request", lambda request, **kw: project)
    handler = _route_handler(module, "POST", "/sonder-editor/project/{project_id}/scenes")
    response = asyncio.run(handler(DummyRequest(match_info={"project_id": "project"},
                                                body={"name": "New"})))
    created = _response_json(response)
    stored = project_manager.load_project(project.project_dir).get_scene(created["scene_id"])
    for family in MEDIA:
        assert _ids(created, family) == _ids(stored, family) and all(_ids(created, family))


# --------------------------------------------------------------------------
# Post-merge validation reads each side's lanes in its own numbering
# --------------------------------------------------------------------------

_EMPTY = Scene(scene_id="scene-1", duration_frames=100).to_dict()


def _overlapping(lanes, lane):
    scene = Scene.from_dict(copy.deepcopy(_EMPTY))
    scene.reference_lane_count = len(lanes)
    scene.reference_lane_configs = [LaneConfig() for _ in lanes]
    scene.reference_lane_recipes = [ReferenceLaneRecipe(lane_id=value) for value in lanes]
    scene.reference_items = [
        ReferenceItem(reference_item_id=item, lane_index=lane, start_frame=start, end_frame=end)
        for item, start, end in (("x", 0, 20), ("y", 10, 30))]
    return Scene.from_dict(scene.to_dict()).to_dict()


def test_an_overlap_the_project_already_had_survives_its_lane_changing_number(tmp_path, monkeypatch):
    target, base = _overlapping(("a", "b", "c"), 2), _overlapping(("b", "c"), 1)
    stored = _overlapping(("b", "c", "late"), 1)
    stored["reference_lane_recipes"].insert(1, stored["reference_lane_recipes"].pop())
    for item in stored["reference_items"]:
        item["lane_index"] = 2
    status, payload, _ = restore_case(monkeypatch, tmp_path, base, target, stored)
    assert status == 200, payload
    ids = [row["lane_id"] for row in payload["scene"]["reference_lane_recipes"]]
    assert ids == ["a", "b", "late", "c"]
    assert {item["lane_index"] for item in payload["scene"]["reference_items"]} == {3}


def test_an_overlap_the_merge_makes_is_still_refused(tmp_path, monkeypatch):
    target = _overlapping(("a", "b", "c"), 2)
    target["reference_items"][0].update(lane_index=0)
    base = copy.deepcopy(target)
    base["reference_items"][0].update(lane_index=1)
    stored = copy.deepcopy(base)
    stored["reference_items"][1].update(lane_index=0)
    status, payload, _ = restore_case(monkeypatch, tmp_path, base, target, stored)
    assert (status, payload["invariant"]) == (409, "reference_overlap")


def test_an_item_stranded_past_the_last_lane_survives_a_lane_coming_back(tmp_path, monkeypatch):
    def scene(count, at):
        value = _legacy_scene(count=count, configs=[{**BLANK, "lane_id": f"v{index}"}
                                                    for index in range(count)])
        value["clips"] = [ClipReference(clip_id="s", track_index=at, timeline_end_frame=8).to_dict()]
        return Scene.from_dict(value).to_dict()
    target, base = scene(3, 5), scene(2, 4)
    stored = copy.deepcopy(base)
    stored["video_lane_count"] = 3
    stored["video_lane_configs"].append({**BLANK, "lane_id": "take"})
    stored["clips"][0]["track_index"] = 5
    status, payload, _ = restore_case(monkeypatch, tmp_path, base, target, stored)
    assert status == 200, payload
    assert payload["scene"]["video_lane_count"] == 4
    assert payload["scene"]["clips"][0]["track_index"] == 6


@pytest.mark.parametrize("field", ["clips", "audio_tracks", "reference_items", "video_lane_configs",
                                   "video_lane_count", "reference_lane_recipes"])
@pytest.mark.parametrize("value", ["x", {"a": 1}, 5, None, [1, "x", None], True])
def test_a_malformed_step_is_refused_not_a_crash(field, value, tmp_path, monkeypatch):
    base = Scene.from_dict(_legacy_scene()).to_dict()
    target = {**copy.deepcopy(base), field: value}
    status, payload, _ = restore_case(monkeypatch, tmp_path, base, target, base)
    assert status in (200, 409), payload
    json.dumps(payload)


# --------------------------------------------------------------------------
# The matrix
# --------------------------------------------------------------------------

class Model:
    """A real scene, and the lanes' true identities beside it. Every change edits both."""

    def __init__(self, family, named):
        self.family, self.d = family, LANE_DESCRIPTORS_BY_LANE_TYPE[family]
        raw = _legacy_scene(family, 4, [{**BLANK, "name": f"L{i}" if named else ""}
                                        for i in range(4)])
        if family == "reference":
            raw["reference_lane_recipes"] = [{"lane_id": f"id{i}"} for i in range(4)]
        self.scene = Scene.from_dict(raw)
        for lane, key in enumerate("ABC"):            # lane 3 is empty
            self.put(key, lane)
        self.tokens = [f"t{i}" for i in range(4)]
        self.fresh = (f"n{i}" for i in itertools.count())

    def put(self, key, lane):
        start = 20 * (ord(key) - 65)
        bounds = dict(timeline_start_frame=start, timeline_end_frame=start + 10)
        if self.family == "audio":
            item = AudioTrack(track_id=key, lane_index=lane, **bounds)
        elif self.family == "reference":
            item = ReferenceItem(reference_item_id=key, lane_index=lane,
                                 start_frame=start, end_frame=start + 10)
        else:
            item = ClipReference(clip_id=key, track_index=lane, source_out_frame=10,
                                 total_source_frames=100, **bounds,
                                 role="render" if self.family == "video" else "motion_driver")
        getattr(self.scene, self.d.items_attr).append(item)

    def saved(self):
        """What a commit stores and the next load reads."""
        self.scene.identify_new_lanes()
        self.scene = Scene.from_dict(_wire(self.scene.to_dict()))
        return self.scene.to_dict()

    def run(self, *operations):
        _batch(self.scene, *operations)

    def items(self):
        return {getattr(item, self.d.item_id_attr): getattr(item, self.d.item_index_attr)
                for item in getattr(self.scene, self.d.items_attr)}

    def settings(self):
        return [routes._normalized_lane_config(row)
                for row in getattr(self.scene, self.d.configs_attr)]

    def config(self, token, **fields):
        self.run({"type": "update_lane_config", "lane_type": self.family,
                  "lane_index": self.tokens.index(token), "fields": fields})

    def add(self):
        self.run({"type": "set_lane_count", "lane_type": self.family,
                  "count": len(self.tokens) + 1})
        self.tokens.append(next(self.fresh))

    def remove(self, token, policy="require_empty", to=None):
        index = self.tokens.index(token)
        expected = {"lane_count": len(self.tokens), "config": self.settings()[index]}
        if self.family == "reference":
            expected["lane_id"] = self.scene.reference_lane_recipes[index].lane_id
        operation = {"type": "remove_lane", "lane_type": self.family, "lane_index": index,
                     "item_policy": policy, "expected": expected}
        if to:
            operation["target_lane"] = self.tokens.index(to)
        self.run(operation)
        self.tokens.remove(token)

    def swap(self, a, b):
        i, j = self.tokens.index(a), self.tokens.index(b)
        rows = self.scene.reference_lane_recipes
        self.run({"type": "move_lane", "lane_type": self.family, "from_index": i, "to_index": j,
                  "expected": {"from_lane_id": rows[i].lane_id, "to_lane_id": rows[j].lane_id}})
        self.tokens[i], self.tokens[j] = b, a

    def move(self, key, token):
        for item in getattr(self.scene, self.d.items_attr):
            if getattr(item, self.d.item_id_attr) == key:
                setattr(item, self.d.item_index_attr, self.tokens.index(token))

    def compact(self, key):
        token, count = self.tokens[self.items()[key]], len(self.tokens)
        self.run({"type": "delete_clip", "clip_id": key} if self.family == "video"
                 else {"type": "delete_audio_track", "track_id": key})
        if getattr(self.scene, self.d.count_attr) < count:
            self.tokens.remove(token)

    def take(self, key="T"):
        """Takes land as a render places them, and on an appended lane elsewhere."""
        if self.family in ("video", "audio"):
            lane = allocate_take_lane(self.scene, self.family, LaneConfig)
            if lane == len(self.tokens):
                self.tokens.append(next(self.fresh))
        else:
            self.add()
            lane = len(self.tokens) - 1
        self.put(key, lane)

    def view(self):
        view = {token: [row, set()] for token, row in zip(self.tokens, self.settings())}
        for key, lane in self.items().items():
            view[self.tokens[lane]][1].add(key)
        return view


def _other_then_take(model):
    model.remove("t2", "delete_items")
    model.take()


STEPS = {
    "lock": lambda m: m.config("t1", locked=True),
    "rename": lambda m: m.config("t1", name="New"),
    "add lane": lambda m: m.add(),
    "remove empty lane": lambda m: m.remove("t3"),
    "remove lane, move items": lambda m: m.remove(
        "t1", "move_items", "t3" if m.family == "motion_driver" else "t0"),
    "remove lane, delete items": lambda m: m.remove("t1", "delete_items"),
    "delete and compact": lambda m: m.compact("B"),
    "drop on new lane": lambda m: (m.add(), m.move("A", m.tokens[-1])),
    "move item": lambda m: m.move("A", "t3"),
    "swap lanes": lambda m: m.swap("t1", "t2"),
}
SINCE = {
    "nothing": lambda m: None,
    "a take": lambda m: m.take(),
    "three takes": lambda m: (m.take("T"), m.take("U"), m.take("V")),
    "another lane renamed": lambda m: m.config("t2", name="Other"),
    "the lane renamed": lambda m: m.config("t1", name="Clash"),
    "the lane locked": lambda m: m.config("t1", locked=True),
    "another lane removed": lambda m: m.remove("t2", "delete_items"),
    "the first lane removed": lambda m: m.remove("t0", "delete_items"),
    "another lane removed, then a take": _other_then_take,
    "two lanes swapped": lambda m: m.swap("t0", "t2"),
}


def _applies(family, step, since):
    if step == "delete and compact" and family not in ("video", "audio"):
        return False
    return family == "reference" or (step != "swap lanes" and since != "two lanes swapped")


CASES = [case for case in itertools.product(
    (*MEDIA, "reference"), (False, True), STEPS, SINCE, ("undo", "redo"))
    if _applies(case[0], case[2], case[3])]


def _three_way(base, target, stored):
    if target == base or stored == target:
        return stored, False
    return (target, False) if stored == base else (stored, True)


def _expected(base, target, stored):
    """(view, refused): lanes by identity, settings by field, items by their lane."""
    refused, view = False, {}
    for token in {*base, *target, *stored}:
        rows = [side[token][0] if token in side else None for side in (base, target, stored)]
        if all(row is not None for row in rows):
            settings = {}
            for key in rows[2]:
                settings[key], clash = _three_way(*(row[key] for row in rows))
                refused |= clash
        else:
            settings, clash = _three_way(*rows)
            refused |= clash
        if settings is not None:
            view[token] = [settings, set()]
    lanes = [{key: token for token, (_, keys) in side.items() for key in keys}
             for side in (base, target, stored)]
    for key in {key for side in lanes for key in side}:
        lane, clash = _three_way(*(side.get(key) for side in lanes))
        refused |= clash
        if lane is not None and lane not in view:
            refused = True
        elif lane is not None:
            view[lane][1].add(key)
    return view, refused


def _shape(view):
    return sorted((json.dumps(row, sort_keys=True), sorted(keys)) for row, keys in view.values())


def _restore(base, target, stored):
    try:
        merged = merge_scene_history(base, target, stored)
        scene = Scene.from_dict(merged)
        routes._validate_scene_history_merge(scene, target, stored)
        return scene
    except SceneMergeConflict:
        return None


@pytest.mark.parametrize(("family", "named", "step", "since", "operation"), CASES)
def test_a_step_reverses_only_its_own_lanes(family, named, step, since, operation):
    model = Model(family, named)
    before, before_view = model.saved(), model.view()
    STEPS[step](model)
    after, after_view = model.saved(), model.view()
    if operation == "redo":
        # Undone cleanly, then something happened, then Redo.
        base, target, base_view, target_view = before, after, before_view, after_view
        model = Model(family, named)
        model.saved()
        model.fresh = (f"later{i}" for i in itertools.count())
    else:
        base, target, base_view, target_view = after, before, after_view, before_view
    try:
        SINCE[since](model)
    except ValueError:
        pytest.skip("the step already removed that lane")
    stored, stored_view = model.saved(), model.view()
    inputs = copy.deepcopy((base, target, stored))
    expected, refused = _expected(base_view, target_view, stored_view)
    # Both reordered the same lanes: no order is right.
    refused |= step == "swap lanes" and since == "two lanes swapped"

    scene = _restore(base, target, stored)

    assert (base, target, stored) == inputs
    assert (scene is None) is refused, (stored_view, expected)
    if scene is None:
        return
    result = Model(family, named)
    result.scene = scene
    result.tokens = [f"r{i}" for i in range(getattr(scene, result.d.count_attr))]
    assert _shape(result.view()) == _shape(expected)
    # A retry of a restore that committed, its receipt forgotten, changes nothing.
    again = _restore(base, target, scene.to_dict())
    assert again is not None and again.to_dict() == scene.to_dict()
