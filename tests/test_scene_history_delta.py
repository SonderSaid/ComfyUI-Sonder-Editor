"""An Undo step's before-state comes from its own commit (Undo Across Takes, Phase 1).

`history_before` is written twice on the server (from a capture, for a mutation; from
two dicts, for a restore) and read once in the browser. These tests hold all three to
one answer, then drive the steps that used to go wrong through the real routes.
"""

import asyncio
import copy
import json
import subprocess
from pathlib import Path

import pytest

from server import scene_history_delta as delta_module
from server.lane_registry import ensure_lane_index
from server.scene_history_merge import MERGED_WRITE_FIELDS
from server.timeline_state import (AudioTrack, ClipReference, GuideFrame, LaneConfig,
                                   PromptSection, ReferenceEntity, ReferenceItem,
                                   ReferenceMember, Scene, TimelineProject)
from test_change_certificates import BATTERY, _clip_expected, _project
from test_history_validation import restore_case
from test_phase43_routes import (DummyRequest, _load_route_module, _response_json,
                                 _route_handler)
from test_reference_timeline import _split_expected
from test_scene_mutation_registration import _dispatcher_op_types

ROOT = Path(__file__).resolve().parents[1]


def _text_document(text, node_id="node"):
    return {"schema": "prompt_document_v1",
            "nodes": [{"type": "text", "node_id": node_id, "text": text}]}


def _rebuild_in_browser(pairs):
    """`sceneBeforeFromDelta` over (after, delta) pairs."""
    script = f"""
        import {{ sceneBeforeFromDelta, isEmptySceneDelta }}
            from {(ROOT / "web/js/scene_history_delta.js").as_uri()!r};
        let text = ''; for await (const chunk of process.stdin) text += chunk;
        const frozen = value => {{ if (value && typeof value === 'object') {{
            Object.freeze(value); Object.values(value).forEach(frozen); }} return value; }};
        const out = JSON.parse(text).map(([after, delta]) =>
            [sceneBeforeFromDelta(frozen(after), frozen(delta)), isEmptySceneDelta(delta)]);
        process.stdout.write(JSON.stringify(out));
    """
    result = subprocess.run(["node", "--input-type=module", "-e", script],
                            input=json.dumps(pairs), capture_output=True, text=True,
                            encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _wire(value):
    return json.loads(json.dumps(value))


def _rich_project(tmp_path):
    """The certificate fixture, with provenance and fields no model knows."""
    project = _project(tmp_path)
    project.metadata["prompt_channel_template"] = "minimax_h3_ref"
    scene = project.scenes[0]
    for clip in scene.clips:
        clip.generation_params = {"workflow": {"1": {"inputs": {"seed": 1, "cfg": 1.0}}}}
        clip.take_metadata = {"0": {"takes": [1, 2]}}
    scene.clips.append(ClipReference(
        clip_id="c3", source_path="media/c.mp4", track_index=1, timeline_start_frame=120,
        timeline_end_frame=160, source_out_frame=40, total_source_frames=300))
    scene.motion_driver_lane_count = 1
    raw = scene.to_dict()
    raw["future_scene"] = {"kept": True}
    raw["clips"][0]["future_clip"] = {"kept": [1]}
    raw["clips"][2]["future_clip"] = "kept too"
    raw["prompt_sections"][0]["future_section"] = "kept"
    project.scenes[0] = Scene.from_dict(raw)
    for asset_id, kind in (("vid", "video"), ("wav", "audio")):
        asset = copy.deepcopy(project.assets[0])
        asset.asset_id, asset.asset_type, asset.path = asset_id, kind, f"media/{asset_id}"
        asset.fps, asset.frame_count, asset.duration_sec = 24, 48, 2.0
        asset.has_audio, asset.duration_checked = False, True
        project.assets.append(asset)
    return project


def _operations(project):
    """label -> operations, covering every operation the dispatcher accepts."""
    scene = project.scenes[0]
    unit = project.prompt_semantic_units[0]
    cases = {label: operations for label, operations, _flags in BATTERY}
    cases.update({
        "lane added": [{"type": "set_lane_count", "lane_type": "video", "count": 3}],
        "lane removed, clips shift": [{
            "type": "remove_lane", "lane_type": "video", "lane_index": 0,
            "item_policy": "move_items", "target_lane": 1,
            "expected": {"lane_count": 2, "config": {
                "name": "A", "color": "", "locked": False, "hidden": False}}}],
        "reference lane moved": [{
            "type": "move_lane", "lane_type": "reference", "from_index": 0, "to_index": 1,
            "expected": {"from_lane_id": "L0", "to_lane_id": "L1"}}],
        "consolidated": [{
            "type": "consolidate_items", "lane_type": "video", "item_ids": ["c1", "c3"],
            "target_lane": 1, "remove_vacated_lanes": True}],
        "clip created": [{"type": "create_clip", "fields": {
            "asset_id": "vid", "timeline_start_frame": 100, "track_index": 0}}],
        "audio created": [{"type": "create_audio_track", "fields": {
            "asset_id": "wav", "timeline_start_frame": 100, "lane_index": 0}}],
        "clip source": [{"type": "replace_clip_source", "clip_id": "c1", "asset_id": "vid"}],
        "audio source": [{"type": "replace_audio_source", "track_id": "t1", "asset_id": "wav"}],
        "clip deleted": [{"type": "delete_clip", "clip_id": "c1"}],
        "audio deleted": [{"type": "delete_audio_track", "track_id": "t1"}],
        "bulk delete": [{"type": "bulk_delete_items", "items": [
            {"type": "clip", "id": "c3"}, {"type": "clip", "id": "c1"}]}],
        "linked": [{"type": "create_link_group", "items": [
            {"type": "clip", "id": "c1"}, {"type": "clip", "id": "c3"}]}],
        "unlinked": [{"type": "unlink_items", "items": [{"type": "clip", "id": "c2"}]}],
        "group deleted": [{"type": "delete_link_group", "group_id": "grp"}],
        "reference created": [{"type": "create_reference_item",
                               "expected": {"next_start_frame": -1},
                               "fields": {"lane_index": 1, "start_frame": 160, "end_frame": -1,
                                          "members": [{"entity_id": "cast", "member_id": "m_aud"}]}}],
        "reference edited": [{"type": "update_reference_item", "reference_item_id": "r1",
                              "fields": {"muted": True}, "expected": {"muted": False}}],
        "reference deleted": [{"type": "delete_reference_item", "reference_item_id": "r1",
                               "expected": scene.reference_items[1].to_dict()}],
        "reference split": [{"type": "split_reference_item", "reference_item_id": "r1",
                             "frame": 100, "expected": _split_expected(scene, "r1")}],
        "guides swapped": [{"type": "swap_guides", "frame_index_a": 10, "frame_index_b": 80,
                            "expected_a": {"guide_id": "g1", "frame_index": 10, "asset_id": "img_a"},
                            "expected_b": {"guide_id": "g2", "frame_index": 80, "asset_id": "img_b"}}],
        "guide moved": [{"type": "move_guide", "from_frame_index": 10, "to_frame_index": 12,
                         "expected": {"guide_id": "g1", "frame_index": 10, "asset_id": "img_a",
                                      "replaces_guide_id": ""}}],
        "guide edited": [{"type": "update_guide", "frame_index": 10,
                          "fields": {"strength": 0.5},
                          "expected": {"guide_id": "g1", "frame_index": 10, "asset_id": "img_a"}}],
        "guide deleted": [{"type": "delete_guide", "frame_index": 10,
                           "expected": {"guide_id": "g1", "frame_index": 10, "asset_id": "img_a"}}],
        "guide created": [{"type": "create_guide", "fields": {
            "frame_index": 33, "asset_id": "img_a", "guide_id": "g3"},
            "expected": {"replaces_guide_id": ""}}],
        "dependencies imported": [{"type": "import_prompt_context_dependencies",
                                   "profiles": [], "semantic_units": []}],
        "identity created": [{"type": "create_prompt_semantic_unit",
                              "handle_suggestion": "Narrator", "unit": {
            "semantic_unit_id": "narrator", "name": "Narrator", "kind": "subject"}}],
        "identity with sources": [{
            "type": "create_prompt_identity_with_sources", "handle_suggestion": "Cowboy",
            "unit": {"semantic_unit_id": "cowboy", "name": "Cowboy", "kind": "subject",
                     "definition": "a cowboy", "attachment_defaults": {},
                     "sources": [{"entity_id": "cast", "member_id": "m_img",
                                  "contribution": ""}]}}],
        "identity kept": [{"type": "delete_prompt_semantic_unit_if_unreferenced",
                           "semantic_unit_id": "hero", "expected": copy.deepcopy(unit)}],
        "global prompt": [{
            "type": "update_scene_fields",
            "fields": {"global_channels": {"summary": "HELLO"},
                       "global_channel_docs": {"summary": _text_document("HELLO")}},
            "expected": {key: copy.deepcopy(scene.to_dict()[key]) for key in (
                "prompt", "global_channels", "global_channel_docs")}}],
    })
    return cases


def _labels(tmp_path_factory=None):
    import tempfile
    return sorted(_operations(_rich_project(Path(tempfile.gettempdir()))))


def test_the_corpus_runs_every_operation_the_dispatcher_accepts(tmp_path):
    named = {operation["type"] for operations in _operations(_rich_project(tmp_path)).values()
             for operation in operations}
    assert named == set(_dispatcher_op_types())


def test_every_operation_describes_the_scene_it_was_applied_to(tmp_path, monkeypatch):
    """The capture, two whole dicts and the browser agree, for every operation."""
    routes = _load_route_module(monkeypatch)
    rows = []
    for label in _labels():
        project = _rich_project(tmp_path)
        before = copy.deepcopy(project.scenes[0].to_dict())
        _changed, payload = routes._apply_scene_mutation_batch(
            project, "scene", copy.deepcopy(_operations(project)[label]))
        after = payload["scene"]
        assert after == project.scenes[0].to_dict(), label
        assert payload["history_before"] == delta_module.delta_between(before, after), label
        rows.append((label, before, after, payload["history_before"]))
    rebuilt = _rebuild_in_browser([[after, delta] for _l, _b, after, delta in rows])
    for (label, before, after, _delta), (scene, empty) in zip(rows, rebuilt):
        assert scene == _wire(before), label
        assert empty is (before == after), label
    # Project-level operations: the scene is not what they change.
    assert {label for label, before, after, _delta in rows if before == after} == {
        "dependencies imported", "identity created", "identity kept", "identity with sources"}


@pytest.mark.parametrize("field", [
    field for field in (*MERGED_WRITE_FIELDS["scalar"], *MERGED_WRITE_FIELDS["mapping"],
                        *(spec[0] for spec in MERGED_WRITE_FIELDS["collection"]))])
def test_every_merged_field_is_described(field, tmp_path):
    before = _rich_project(tmp_path).scenes[0].to_dict()
    after = copy.deepcopy(before)
    value = after[field]
    after[field] = (value[:-1] if isinstance(value, list) and value
                    else [{"probe_id": 1}] if isinstance(value, list)
                    else {**value, "probe": {"a": 1}} if isinstance(value, dict)
                    else "probe" if isinstance(value, str) else value + 1)
    [(rebuilt, empty)] = _rebuild_in_browser(
        [[after, delta_module.delta_between(before, after)]])
    assert rebuilt == _wire(before) and empty is False


def test_keys_that_appear_or_go_are_described(tmp_path):
    """An unknown field another version adds or drops, on the scene and on a member."""
    before = _rich_project(tmp_path).scenes[0].to_dict()
    after = copy.deepcopy(before)
    after["future_scene_2"] = 1
    del after["future_scene"]
    after["clips"][0]["future_clip_2"] = 1
    del after["clips"][2]["future_clip"]
    after["prompt_sections"][0]["future_section_2"] = 1
    for one, other in ((before, after), (after, before)):
        [(rebuilt, _empty)] = _rebuild_in_browser([[other, delta_module.delta_between(one, other)]])
        assert rebuilt == _wire(one)


def _captured(scene, mutate):
    before = copy.deepcopy(scene.to_dict())
    captured = delta_module.capture(scene)
    assert captured is not None
    mutate(scene)
    after = scene.to_dict()
    delta = delta_module.delta_from_capture(captured, scene, after)
    assert delta == delta_module.delta_between(before, after)
    return before, after, delta


def _nested(scene):
    scene.clips[0].generation_params["workflow"]["1"]["inputs"]["seed"] = 2


def _nested_group(scene):
    scene.linked_item_groups[0]["items"].pop()


def _reorder(scene):
    scene.clips.reverse()


def _duplicate(scene):
    scene.clips.append(copy.deepcopy(scene.clips[0]))


def _blank_id(scene):
    scene.audio_tracks[0].track_id = ""


def _unknown_gone(scene):
    del scene.clips[2]


@pytest.mark.parametrize("mutate", [_nested, _nested_group, _reorder, _duplicate, _blank_id,
                                    _unknown_gone])
def test_the_capture_is_independent_of_the_scene(mutate, tmp_path):
    before, after, delta = _captured(_rich_project(tmp_path).scenes[0], mutate)
    assert before != after and not delta_module.is_empty(delta)
    [(rebuilt, _empty)] = _rebuild_in_browser([[after, delta]])
    assert rebuilt == _wire(before)


def test_a_removed_member_keeps_the_fields_no_model_knows(tmp_path):
    _before, _after, delta = _captured(_rich_project(tmp_path).scenes[0], _unknown_gone)
    [removed] = delta["members"]["clips"]["removed"]
    assert removed["clip_id"] == "c3" and removed["future_clip"] == "kept too"


def test_scenes_that_already_repeat_an_id_travel_whole(tmp_path):
    scene = _rich_project(tmp_path).scenes[0]
    scene.clips.append(copy.deepcopy(scene.clips[0]))
    before, _after, delta = _captured(scene, lambda value: value.clips.pop())
    assert delta["fields"]["clips"] == before["clips"]


def test_equal_values_are_no_change(tmp_path):
    def retype(scene):
        scene.clips[0].opacity = 1 if isinstance(scene.clips[0].opacity, float) else 1.0
        scene.clips[0].source_path = "".join(list(scene.clips[0].source_path))
    _before, _after, delta = _captured(_rich_project(tmp_path).scenes[0], retype)
    assert delta_module.is_empty(delta)


def test_a_moved_clip_carries_one_field_not_its_provenance(tmp_path):
    scene = _rich_project(tmp_path).scenes[0]
    scene.clips[0].generation_params = {"workflow": "x" * 50_000}
    def shift(value):
        for clip in value.clips:
            clip.track_index += 1
    _before, _after, delta = _captured(scene, shift)
    assert len(json.dumps(delta)) < 1000


@pytest.mark.parametrize("broken", ["capture", "delta"])
def test_history_that_cannot_be_built_never_fails_the_write(broken, tmp_path, monkeypatch):
    routes = _load_route_module(monkeypatch)
    project = _rich_project(tmp_path)

    def explode(*_args, **_kwargs):
        raise RuntimeError("history")

    if broken == "capture":
        monkeypatch.setattr(delta_module, "_Capture", explode)
    else:
        monkeypatch.setattr(delta_module, "_captured_delta", explode)
    changed, payload = routes._apply_scene_mutation_batch(
        project, "scene", [{"type": "update_scene_fields", "fields": {"name": "Saved"}}])
    assert changed and payload["scene"]["name"] == "Saved" and "history_before" not in payload


def test_a_write_that_changed_nothing_says_so(tmp_path, monkeypatch):
    routes = _load_route_module(monkeypatch)
    project = _rich_project(tmp_path)
    changed, payload = routes._apply_scene_mutation_batch(
        project, "scene", [{"type": "update_scene_fields", "fields": {"name": "Saloon"}}])
    assert changed is False and delta_module.is_empty(payload["history_before"])


def test_the_browser_refuses_a_delta_it_cannot_read(tmp_path):
    before = _rich_project(tmp_path).scenes[0].to_dict()
    after = copy.deepcopy(before)
    after["clips"] = after["clips"][1:]
    good = delta_module.delta_between(before, after)
    bad = []
    for change in ({"v": 2}, {"members": None}, {"fields": []}, {"absent": None}):
        bad.append({**copy.deepcopy(good), **change})
    for part, value in (("absent", 5), ("absent", {}), ("fields", 5), ("fields", [1])):
        odd = copy.deepcopy(good)
        odd["members"]["clips"][part] = {good["members"]["clips"]["order"][1]: value}
        bad.append(odd)
    missing = copy.deepcopy(good)
    missing["members"]["clips"]["removed"] = []
    unknown = copy.deepcopy(good)
    unknown["members"]["other"] = unknown["members"].pop("clips")
    cases = [*bad, missing, unknown, None, [], "x"]
    for scene, empty in _rebuild_in_browser([[after, case] for case in cases]):
        assert scene is None and empty is False


# --------------------------------------------------------------------------
# The steps that went wrong, through the real routes
# --------------------------------------------------------------------------

def _land_take(scene, clip_id="take"):
    index = scene.video_lane_count
    ensure_lane_index(scene, "video", index, LaneConfig)
    scene.clips.append(ClipReference(clip_id=clip_id, timeline_start_frame=0,
                                     timeline_end_frame=8, track_index=index))


class _Server:
    """One saved scene behind the real mutation batch and the real restore route."""

    def __init__(self, monkeypatch, tmp_path, scene, references=()):
        self.routes = _load_route_module(monkeypatch)
        self.project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                                       scenes=[scene], references=list(references))
        self.reload()
        monkeypatch.setattr(self.routes, "_load_project_from_request",
                            lambda request, **kwargs: self.project)
        monkeypatch.setattr(self.routes, "save_project", lambda project, **kwargs: None)

    def reload(self):
        """Save and load: a lane created since has its id from then on."""
        scene = self.project.scenes[0]
        scene.identify_new_lanes()
        self.project.scenes[0] = Scene.from_dict(_wire(scene.to_dict()))

    @property
    def scene(self):
        return self.project.scenes[0]

    def write(self, *operations):
        _changed, payload = self.routes._apply_scene_mutation_batch(
            self.project, "scene-1", list(operations))
        self.reload()
        return self.step(payload)

    @staticmethod
    def step(payload):
        """(before, after) as the editor keeps them."""
        [(before, _empty)] = _rebuild_in_browser([[payload["scene"], payload["history_before"]]])
        return before, _wire(payload["scene"])

    def restore(self, base, target):
        handler = _route_handler(
            self.routes, "PUT", "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
        self.token = self.routes._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
        self.request = DummyRequest(
            match_info={"project_id": "project", "scene_id": "scene-1"},
            body={"base_scene": base, "target_scene": target, "restore_token": self.token})
        response = asyncio.run(handler(self.request))
        payload = _response_json(response)
        if response.status == 200:
            self.reload()
        return response.status, payload

    def undo(self, step):
        before, after = step
        return self.restore(after, before)


def _scene(**values):
    values.setdefault("video_lane_count", 2)
    values.setdefault("video_lane_configs", [LaneConfig(), LaneConfig()])
    return Scene(scene_id="scene-1", duration_frames=24, **values)


LOCK = {"type": "update_lane_config", "lane_type": "video", "lane_index": 0,
        "fields": {"locked": True}}


@pytest.mark.parametrize("operation", [
    LOCK, {"type": "update_scene_fields", "fields": {"duration_frames": 30}}])
def test_undo_and_redo_keep_a_take_the_editor_never_read(operation, monkeypatch, tmp_path):
    server = _Server(monkeypatch, tmp_path, _scene())
    _land_take(server.scene)
    server.reload()
    step = server.write(operation)
    after_write = server.scene.to_dict()

    status, undone = server.undo(step)
    assert status == 200, undone
    assert [clip["clip_id"] for clip in undone["scene"]["clips"]] == ["take"]
    assert undone["scene"]["video_lane_count"] == 3
    assert undone["scene"]["duration_frames"] == 24
    assert not any(config["locked"] for config in undone["scene"]["video_lane_configs"])

    _land_take(server.scene, "take-2")
    server.reload()
    status, redone = server.undo(server.step(undone))
    assert status == 200, redone
    assert [clip["clip_id"] for clip in redone["scene"]["clips"]] == ["take", "take-2"]
    assert redone["scene"]["duration_frames"] == after_write["duration_frames"]
    assert redone["scene"]["video_lane_configs"][0] == after_write["video_lane_configs"][0]

    status, again = server.undo(server.step(redone))
    assert status == 200, again
    assert [clip["clip_id"] for clip in again["scene"]["clips"]] == ["take", "take-2"]
    assert again["scene"]["duration_frames"] == 24


def test_a_redo_paints_from_a_scene_that_holds_the_take(monkeypatch, tmp_path):
    """The reverse step's target is what the editor paints: it must hold the take."""
    server = _Server(monkeypatch, tmp_path, _scene())
    step = server.write({"type": "update_scene_fields", "fields": {"duration_frames": 30}})
    _land_take(server.scene)
    server.reload()
    _status, undone = server.undo(step)
    redo_target, _redo_base = server.step(undone)
    assert redo_target["video_lane_count"] == 3
    assert [clip["clip_id"] for clip in redo_target["clips"]] == ["take"]


def test_only_the_answer_of_the_committing_request_carries_the_pair(monkeypatch, tmp_path):
    server = _Server(monkeypatch, tmp_path, _scene())
    step = server.write({"type": "update_scene_fields", "fields": {"name": "After"}})
    status, first = server.undo(step)
    assert status == 200 and not delta_module.is_empty(first["history_before"])
    handler = _route_handler(
        server.routes, "PUT", "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    get = _route_handler(
        server.routes, "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore-token/{token}")
    info = {"project_id": "project", "scene_id": "scene-1", "token": server.token}
    for durable in (True, False):
        if not durable:
            monkeypatch.setattr(server.routes, "has_durable_scene_restore_receipt",
                                lambda *args: False)
        for response in (asyncio.run(handler(server.request)),
                         asyncio.run(get(DummyRequest(match_info=info)))):
            assert response.status == 200
            assert "history_before" not in _response_json(response)


def test_a_retried_commit_describes_the_document_it_committed_over(monkeypatch, tmp_path):
    from server.project_manager import ProjectVersionConflict
    server = _Server(monkeypatch, tmp_path, _scene())
    before, after = server.write(
        {"type": "update_scene_fields", "fields": {"duration_frames": 30}})
    newer = TimelineProject.from_dict(_wire(server.project.to_dict()))
    newer.project_dir = str(tmp_path)
    _land_take(newer.scenes[0])
    saves = []
    inputs = copy.deepcopy((before, after))

    def save(project, **kwargs):
        saves.append(project)
        if len(saves) == 1:
            raise ProjectVersionConflict(project_dir=str(tmp_path), expected_modified_at="1",
                                         actual_modified_at="2", current_data={})

    monkeypatch.setattr(server.routes, "save_project", save)
    monkeypatch.setattr(server.routes, "load_project", lambda path: newer)
    status, undone = server.restore(after, before)
    assert status == 200 and len(saves) == 2
    assert (before, after) == inputs
    redo_target, redo_base = server.step(undone)
    assert [clip["clip_id"] for clip in redo_target["clips"]] == ["take"]
    assert [clip["clip_id"] for clip in redo_base["clips"]] == ["take"]


def test_the_mutation_route_sends_the_before_state_of_the_attempt_that_committed(
        monkeypatch, tmp_path):
    """Through the HTTP handler: the first attempt loses its race to a take."""
    from server.project_manager import ProjectVersionConflict, create_project, load_project
    from server import project_manager
    routes = _load_route_module(monkeypatch)
    project = create_project("Race", base_dir=str(tmp_path))
    if not project.scenes:
        project.scenes = [_scene()]
    scene_id = project.scenes[0].scene_id
    project_manager.save_project(project, notify=False)
    real_save, calls = routes.save_project, []

    def save(candidate, **kwargs):
        calls.append(candidate)
        if len(calls) == 1:
            other = load_project(project.project_dir)
            _land_take(other.scenes[0])
            project_manager.save_project(other, notify=False)
        return real_save(candidate, **kwargs)

    monkeypatch.setattr(routes, "save_project", save)
    monkeypatch.setattr(routes, "_load_project_from_request",
                        lambda request, **kwargs: load_project(project.project_dir))
    handler = _route_handler(
        routes, "POST", "/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "Race", "scene_id": scene_id},
        body={"operations": [{"type": "update_scene_fields", "fields": {"name": "Won"}}]})))
    payload = _response_json(response)
    assert response.status == 200 and len(calls) == 2, payload
    before, after = _Server.step(payload)
    assert (before["name"] != "Won", after["name"]) == (True, "Won")
    for scene in (before, after):
        assert [clip["clip_id"] for clip in scene["clips"]] == ["take"]


def test_a_writing_apply_undoes_and_redoes(monkeypatch, tmp_path):
    """MT-024: new global documents and replaced sections in one batch."""
    server = _Server(monkeypatch, tmp_path, _scene(prompt_sections=[
        PromptSection.from_dict({"prompt_id": "s", "start_frame": 0, "end_frame": 10})]))
    before_apply = _wire(server.scene.to_dict())
    scene = server.scene.to_dict()
    sections = copy.deepcopy(scene["prompt_sections"])
    sections[0]["channel_docs"]["summary"] = _text_document("SECTION")
    sections[0]["channels"]["summary"] = "SECTION"
    step = server.write(
        {"type": "replace_prompt_sections", "sections": sections,
         "expected": {"sections": scene["prompt_sections"]}},
        {"type": "update_scene_fields", "fields": {
            "global_channels": {"summary": "GLOBAL", "overall_soundscape": "WIND"},
            "global_channel_docs": {"summary": _text_document("GLOBAL", "a"),
                                    "overall_soundscape": _text_document("WIND", "b")}},
         "expected": {name: scene[name] for name in (
             "prompt", "global_channels", "global_channel_docs")}})
    applied = _wire(server.scene.to_dict())
    for _round in range(2):
        status, undone = server.undo(step)
        assert status == 200, undone
        assert _wire(server.scene.to_dict()) == before_apply
        status, redone = server.undo(server.step(undone))
        assert status == 200, redone
        assert _wire(server.scene.to_dict()) == applied
        step = server.step(redone)


# --------------------------------------------------------------------------
# A prompt document and its mirror
# --------------------------------------------------------------------------

def _section_edit(server, key):
    section = server.scene.to_dict()["prompt_sections"][0]
    return server.write({
        "type": "update_prompt_section", "index": 0,
        "fields": {"channels": {**section["channels"], key: "HELLO"},
                   "channel_docs": {**section["channel_docs"], key: _text_document("HELLO")}},
        "expected": {name: section[name] for name in ("prompt_id", "channels", "channel_docs")}})


def _global_edit(server, key):
    scene = server.scene.to_dict()
    return server.write({
        "type": "update_scene_fields",
        "fields": {"global_channels": {key: "HELLO"},
                   "global_channel_docs": {key: _text_document("HELLO")}},
        "expected": {name: scene[name] for name in (
            "prompt", "global_channels", "global_channel_docs")}})


@pytest.mark.parametrize("edit", [_section_edit, _global_edit])
@pytest.mark.parametrize("key", ["summary", "visual"])
@pytest.mark.parametrize("pinned", [False, True])
def test_first_text_in_a_channel_undoes_and_redoes(edit, key, pinned, monkeypatch, tmp_path):
    server = _Server(monkeypatch, tmp_path, _scene(prompt_sections=[
        PromptSection.from_dict({"prompt_id": "s", "start_frame": 0, "end_frame": 10})]))
    before_edit = _wire(server.scene.to_dict())
    step = edit(server, key)
    assert "HELLO" in json.dumps(server.scene.to_dict())

    status, undone = server.undo(step)
    assert status == 200, undone
    for source in (undone["scene"], server.scene.to_dict()):
        assert "HELLO" not in json.dumps(source)
        assert _wire(source)["prompt_sections"] == before_edit["prompt_sections"]
        assert source["global_channel_docs"] == before_edit["global_channel_docs"]
        assert source["global_channels"] == before_edit["global_channels"]

    # The pinned base is what a lost response leaves: (restore target, the step's result).
    target, base = (step[1], step[0]) if pinned else server.step(undone)
    status, redone = server.restore(base, target)
    assert status == 200, redone
    assert "HELLO" in json.dumps(server.scene.to_dict())


def test_removing_a_document_never_drops_text_another_writer_added(monkeypatch, tmp_path):
    server = _Server(monkeypatch, tmp_path, _scene(prompt_sections=[
        PromptSection.from_dict({"prompt_id": "s", "start_frame": 0, "end_frame": 10})]))
    step = _section_edit(server, "summary")
    section = server.scene.prompt_sections[0]
    section.set_channel_documents({**section.channel_docs, "dialogue": _text_document("OTHER")})
    server.reload()
    status, undone = server.undo(step)
    assert status == 200, undone
    channels = undone["scene"]["prompt_sections"][0]["channels"]
    assert channels["dialogue"] == "OTHER" and "summary" not in channels


# --------------------------------------------------------------------------
# Members the Library no longer has
# --------------------------------------------------------------------------

def _item(members, start=0, end=10, item_id="I"):
    return ReferenceItem(reference_item_id=item_id, lane_index=0, start_frame=start,
                         end_frame=end,
                         members=[{"entity_id": "e", "member_id": value} for value in members])


def _library(*members):
    return [ReferenceEntity(reference_id="e", name="E", members=[
        ReferenceMember(member_id=value) for value in members])]


def _with_items(scene, *items, duration=24):
    copy_scene = Scene.from_dict(_wire(scene.to_dict()))
    copy_scene.reference_items = list(items)
    copy_scene.duration_frames = duration
    return _wire(copy_scene.to_dict())


# base, target, stored as item tuples; the Library; expected (status, items, dropped)
MEMBER_ROWS = {
    "undo of a delete, only member gone": (
        [], [("m1",)], [], ["m2"], 200, [], 1),
    "undo of a delete, one of two gone": (
        [], [("m1", "m2")], [], ["m2"], 200, [("m2",)], 1),
    "undo of a delete, all present": (
        [], [("m1", "m2")], [], ["m1", "m2"], 200, [("m1", "m2")], 0),
    "undo of a swap whose member is gone": (
        [("m2",)], [("m1",)], [("m2",)], ["m2"], 200, [("m2",)], 1),
    "undo of an append whose other member is gone": (
        [("m1", "m3")], [("m1",)], [("m3",)], ["m3"], 200, [("m3",)], 1),
    "undo of a removal, the remover's other member gone": (
        [("m1", "m2")], [("m1", "m2", "m3")], [("m2",)], ["m2", "m3"], 200, [("m2", "m3")], 0),
    "redo of a swap to a member that is gone removed the item": (
        [("m1",)], [("m2",)], [], ["m2"], 200, [("m2",)], 0),
    "redo of a swap that also adds a member that is gone": (
        [("m1",)], [("m2", "m4")], [], ["m2"], 200, [("m2",)], 1),
    "an item the Library delete removed": (
        [("m1",)], [("m1",)], [], ["m2"], 200, [], 1),
    "an item another edit deleted": (
        [("m1",)], [("m1", "m2")], [], ["m1", "m2"], 409, None, 0),
    "a dangling member the stored item still holds": (
        [("m1", "m2")], [("m1",)], [("m1", "m2")], ["m2"], 200, [("m1",)], 0),
}


@pytest.mark.parametrize("label", sorted(MEMBER_ROWS))
def test_a_step_never_restores_a_member_the_library_lost(label, monkeypatch, tmp_path):
    base, target, stored, library, expected_status, expected, dropped = MEMBER_ROWS[label]
    server = _Server(monkeypatch, tmp_path, _scene(), _library(*library))
    # A step over an unmoved item still has to differ somewhere to be a step.
    base_scene = _with_items(server.scene, *[_item(value) for value in base], duration=30)
    target_scene = _with_items(server.scene, *[_item(value) for value in target])
    server.project.scenes[0] = Scene.from_dict(
        _with_items(server.scene, *[_item(value) for value in stored], duration=30))
    inputs = copy.deepcopy((base_scene, target_scene))
    status, payload = server.restore(base_scene, target_scene)
    assert status == expected_status, payload
    assert (base_scene, target_scene) == inputs
    assert payload.get("dropped_reference_members", 0) == dropped
    if expected is None:
        return
    assert [tuple(member["member_id"] for member in item.members)
            for item in server.scene.reference_items] == expected
    assert server.scene.duration_frames == 24
    # What the Undo did is what its Redo reverses.
    if not delta_module.is_empty(payload["history_before"]):
        status, redone = server.undo(server.step(payload))
        assert status == 200, redone
        assert [tuple(member["member_id"] for member in item.members)
                for item in server.scene.reference_items] == list(stored)


def test_a_move_step_survives_the_members_changing_since(monkeypatch, tmp_path):
    """Move I{m1}, append m3, the Library deletes m1: undoing the move still moves it."""
    server = _Server(monkeypatch, tmp_path, _scene(), _library("m3"))
    base = _with_items(server.scene, _item(("m1",), start=10, end=20))
    target = _with_items(server.scene, _item(("m1",), start=0))
    server.project.scenes[0] = Scene.from_dict(
        _with_items(server.scene, _item(("m3",), start=10, end=20)))
    status, payload = server.restore(base, target)
    assert status == 200, payload
    [item] = server.scene.reference_items
    assert (item.start_frame, [m["member_id"] for m in item.members]) == (0, ["m3"])
    assert "dropped_reference_members" not in payload


def test_a_duration_step_survives_the_members_changing_since(monkeypatch, tmp_path):
    server = _Server(monkeypatch, tmp_path, _scene(), _library("m3"))
    base = _with_items(server.scene, _item(("m1",), end=-1), duration=200)
    target = _with_items(server.scene, _item(("m1",), end=-1), duration=100)
    server.project.scenes[0] = Scene.from_dict(
        _with_items(server.scene, _item(("m3",), end=-1), duration=200))
    status, payload = server.restore(base, target)
    assert status == 200, payload
    assert server.scene.duration_frames == 100


def test_a_receipt_repeats_the_notice(monkeypatch, tmp_path):
    server = _Server(monkeypatch, tmp_path, _scene(), _library("m2"))
    base = _with_items(server.scene, duration=30)
    target = _with_items(server.scene, _item(("m1",)))
    server.project.scenes[0] = Scene.from_dict(base)
    status, payload = server.restore(base, target)
    assert status == 200 and payload["dropped_reference_members"] == 1
    put = _route_handler(
        server.routes, "PUT", "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    get = _route_handler(
        server.routes, "GET",
        "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore-token/{token}")
    info = {"project_id": "project", "scene_id": "scene-1", "token": server.token}
    for response in (asyncio.run(put(server.request)),
                     asyncio.run(get(DummyRequest(match_info=info)))):
        assert _response_json(response)["dropped_reference_members"] == 1
