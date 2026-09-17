"""History validation contracts, including ordinary-route parity boundaries."""
import asyncio
import copy

import pytest

from test_phase43_routes import (
    DummyRequest, _load_route_module, _route_handler, _response_json,
    _scene_restore_invariant_case,
)
from server.timeline_state import Scene, TimelineProject, GuideFrame, PromptSection
import server.scene_history_merge as history_merge


def restore_case(monkeypatch, tmp_path, base, target, stored):
    module = _load_route_module(monkeypatch)
    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[Scene.from_dict(copy.deepcopy(stored))])
    monkeypatch.setattr(module, "_load_project_from_request", lambda request, **kw: project)
    monkeypatch.setattr(module, "save_project", lambda project, **kw: None)
    handler = _route_handler(module, "PUT", "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target, "restore_token": token})))
    return response.status, _response_json(response), project


@pytest.mark.parametrize("case,code", [
    ("empty_lane", "lane_family_empty"), ("prompt", "prompt_overlap"),
    ("guide", "guide_frame_duplicate"),
])
def test_history_refusal_discloses_invariant_and_participants(case, code, tmp_path, monkeypatch):
    status, payload, _ = restore_case(monkeypatch, tmp_path, *_scene_restore_invariant_case(case))
    assert status == 409
    assert payload["invariant"] == code
    details = payload["details"]
    assert set(details) == {"item_type", "item_id", "other_item_id", "lane_type", "lane_index", "bounds"}
    if case in {"prompt", "guide"}:
        assert {details["item_id"], details["other_item_id"]} == {"edited", "concurrent"}
        assert all(value in payload["error"] for value in ("edited", "concurrent", "frames"))


def test_guide_order_is_normalized_after_history_merge(tmp_path, monkeypatch):
    base = Scene(scene_id="scene-1", duration_frames=24, guide_frames=[
        GuideFrame(guide_id="a", frame_index=2), GuideFrame(guide_id="b", frame_index=8)]).to_dict()
    target = copy.deepcopy(base)
    target["guide_frames"][0]["frame_index"] = 12
    status, payload, _ = restore_case(monkeypatch, tmp_path, base, target, base)
    assert status == 200
    assert [item["guide_id"] for item in payload["scene"]["guide_frames"]] == ["b", "a"]


def test_ordinary_linked_prompt_validation_still_refuses_duration_overflow(monkeypatch):
    module = _load_route_module(monkeypatch)
    scene = Scene(duration_frames=10)
    prompt = PromptSection(0, 12)
    with pytest.raises(module.ProjectMutationRequestError) as exc:
        module._validate_prompt_target_ranges(scene, {prompt.prompt_id: (prompt, 0, 12)})
    assert exc.value.code == "invalid_range"


@pytest.mark.parametrize("category,field,before,after", [
    ("scalar", "name", "before", "after"),
    ("mapping", "global_channel_docs", {}, {"main": {"text": "after"}}),
    ("collection", "clips", [], [{"clip_id": "new"}]),
    ("bundle", "video_lane_count", 1, 2),
])
def test_declared_write_contract_drives_executor(monkeypatch, category, field, before, after):
    base = {field: before}
    target = {field: after}
    assert field in history_merge.scene_history_write_fields()
    assert history_merge.merge_scene_history(base, target, base)[field] == after
    # Removing a rule must remove its write, not only its advertised capability.
    monkeypatch.setitem(history_merge.MERGED_WRITE_FIELDS, category, ())
    assert field not in history_merge.scene_history_write_fields()
    assert history_merge.merge_scene_history(base, target, base)[field] == before


def test_restore_token_serves_merge_capabilities(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = TimelineProject(project_id="project", project_dir=str(tmp_path), scenes=[Scene(scene_id="scene-1")])
    monkeypatch.setattr(module, "_load_project_from_request", lambda request, **kw: project)
    handler = _route_handler(module, "POST", "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore-token")
    response = asyncio.run(handler(DummyRequest(match_info={"project_id": "project", "scene_id": "scene-1"})))
    payload = _response_json(response)
    assert payload["restore_token"]
    assert payload["merged_write_fields"] == history_merge.scene_history_write_fields()
    assert payload["merged_derived_fields"] == list(history_merge.MERGED_DERIVED_FIELDS)
    assert not {"fps", "width", "height", "batch_config"}.intersection(payload["merged_write_fields"])


def test_raw_link_refusal_names_group_and_missing_member(monkeypatch):
    module = _load_route_module(monkeypatch)
    raw = Scene().to_dict()
    raw["linked_item_groups"] = [{"group_id": "broken", "items": [
        {"type": "clip", "id": "missing"}, {"type": "audio", "id": "also-missing"}]}]
    with pytest.raises(module.ProjectMutationRequestError) as exc:
        module._validate_scene_history_link_groups(raw)
    assert exc.value.code == "link_member_unavailable"
    assert exc.value.details["item_id"] == "missing"
    assert exc.value.details["other_item_id"] == "broken"


def test_scene_restore_accepts_a_clip_stranded_by_a_merged_duration_shrink(tmp_path, monkeypatch):
    """Accepted: concurrent clip remains visible/editable beyond the shortened
    scene, but renders nothing there. History preserves work instead of clamping.
    """
    status, payload, _ = restore_case(monkeypatch, tmp_path, *_scene_restore_invariant_case("duration"))
    assert status == 200
    assert payload["scene"]["duration_frames"] == 12
    clip = payload["scene"]["clips"][0]
    assert (clip["timeline_start_frame"], clip["timeline_end_frame"]) == (16, 20)


@pytest.mark.parametrize("kind", ["prompt", "reference"])
@pytest.mark.parametrize("origin", ["manufactured", "stored", "target", "different_geometry"])
def test_overlap_participant_exemption(kind, origin, tmp_path, monkeypatch):
    from server.timeline_state import ReferenceItem
    field = "prompt_sections" if kind == "prompt" else "reference_items"
    def member(member_id, start, end):
        if kind == "prompt":
            item = PromptSection(start, end)
            item.prompt_id = member_id
        else:
            item = ReferenceItem(reference_item_id=member_id, start_frame=start, end_frame=end)
        return item.to_dict()
    base = Scene(scene_id="scene-1", duration_frames=24).to_dict()
    base[field] = [member("edited", 0, 4)]
    target = copy.deepcopy(base)
    target[field][0]["end_frame"] = 8
    stored = copy.deepcopy(base)
    stored[field].append(member("concurrent", 4, 8))
    if origin == "stored":
        base[field][0]["end_frame"] = stored[field][0]["end_frame"] = 8
        target = copy.deepcopy(base)
        target["name"] = "Before"
    elif origin == "target":
        target[field].append(member("concurrent", 4, 8))
        stored = copy.deepcopy(base)
    elif origin == "different_geometry":
        base[field][0]["end_frame"] = stored[field][0]["end_frame"] = 6
    inputs = copy.deepcopy((base, target, stored))
    status, payload, _ = restore_case(monkeypatch, tmp_path, base, target, stored)
    assert (base, target, stored) == inputs
    assert status == (409 if origin == "manufactured" else 200), payload
    if status == 409:
        assert payload["invariant"] == kind + "_overlap"
        assert {payload["details"]["item_id"], payload["details"]["other_item_id"]} == {"edited", "concurrent"}


@pytest.mark.parametrize("kind", ["clip", "audio", "reference"])
@pytest.mark.parametrize("inherited", [False, True])
def test_lane_membership_checks_only_manufactured_violation(kind, inherited, tmp_path, monkeypatch):
    from server.timeline_state import ClipReference, AudioTrack, ReferenceItem
    collection, index_field, count_field, configs_field, item = {
        "clip": ("clips", "track_index", "video_lane_count", "video_lane_configs", ClipReference(clip_id="item", timeline_end_frame=4)),
        "audio": ("audio_tracks", "lane_index", "audio_lane_count", "audio_lane_configs", AudioTrack(track_id="item", timeline_end_frame=4)),
        "reference": ("reference_items", "lane_index", "reference_lane_count", "reference_lane_configs", ReferenceItem(reference_item_id="item", end_frame=4)),
    }[kind]
    base = Scene(scene_id="scene-1", duration_frames=24).to_dict()
    base[count_field] = 3
    base[configs_field] = [{}, {}, {}]
    base[collection] = [item.to_dict()]
    target = copy.deepcopy(base)
    target[collection][0][index_field] = 2
    stored = copy.deepcopy(base)
    stored[count_field] = 1
    stored[configs_field] = [{}]
    if inherited:
        base[count_field] = target[count_field] = 1
        base[configs_field] = target[configs_field] = [{}]
    status, payload, _ = restore_case(monkeypatch, tmp_path, base, target, stored)
    assert status == (200 if inherited else 409), payload
    if status == 409:
        assert payload["invariant"] == kind + "_lane_missing"
        assert payload["details"]["item_id"] == "item"


def test_driver_violation_is_unconditional_even_when_inherited(monkeypatch):
    from server.timeline_state import ClipReference
    module = _load_route_module(monkeypatch)
    scene = Scene(clips=[ClipReference(clip_id="a", role="motion_driver", timeline_end_frame=4),
                         ClipReference(clip_id="b", role="motion_driver", timeline_end_frame=4)])
    raw = scene.to_dict()
    with pytest.raises(module.ProjectMutationRequestError) as exc:
        module._validate_scene_history_merge(scene, raw, raw)
    assert exc.value.code == "driver_lane_occupied"


def test_negative_duration_and_reversed_prompt_are_differential(monkeypatch):
    module = _load_route_module(monkeypatch)
    for scene, code in [(Scene(duration_frames=-1), "scene_duration_negative"),
                        (Scene(prompt_sections=[PromptSection(8, 4)]), "invalid_range")]:
        raw = scene.to_dict()
        module._validate_scene_history_merge(scene, raw, raw)
        with pytest.raises(module.ProjectMutationRequestError) as exc:
            module._validate_scene_history_merge(scene, Scene().to_dict(), Scene().to_dict())
        assert exc.value.code == code


def test_differential_check_never_constructs_another_scene(monkeypatch):
    module = _load_route_module(monkeypatch)
    a, b = PromptSection(0, 8), PromptSection(4, 12)
    scene = Scene(prompt_sections=[a, b])
    raw = scene.to_dict()
    monkeypatch.setattr(module.Scene, "from_dict", lambda *args: pytest.fail("Full Scene reconstruction"))
    module._validate_scene_history_merge(scene, raw, raw)


def test_the_route_restamps_scene_id_over_whatever_the_merge_returned(monkeypatch, tmp_path):
    """The `identity` class, probed at the line that implements it.

    A first version of this passed `scene_id: "impostor"` in the base and target
    documents and asserted the restore still landed on `scene-1`. That proved
    nothing: `get_scene` matches the URL id exactly, `scene_id` is in no write
    list, and the merge returns stored's value untouched -- so the re-stamp was a
    no-op on that input and deleting it would not have failed the test.

    The only way to exercise the re-stamp is to make the merge hand the route a
    document with the wrong id, which no ordinary input can do.
    """
    module = _load_route_module(monkeypatch)
    real_merge = module.merge_scene_history

    def merge_returning_a_foreign_id(base, target, stored):
        merged = real_merge(base, target, stored)
        merged["scene_id"] = "impostor"
        return merged

    monkeypatch.setattr(module, "merge_scene_history", merge_returning_a_foreign_id)

    stored = Scene(scene_id="scene-1", duration_frames=24, name="after").to_dict()
    base = copy.deepcopy(stored)
    target = copy.deepcopy(stored)
    target["name"] = "before"

    project = TimelineProject(project_id="project", project_dir=str(tmp_path),
                              scenes=[Scene.from_dict(copy.deepcopy(stored))])
    monkeypatch.setattr(module, "_load_project_from_request", lambda request, **kw: project)
    monkeypatch.setattr(module, "save_project", lambda project, **kw: None)
    handler = _route_handler(
        module, "PUT", "/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    token = module._SCENE_RESTORE_RECEIPTS.issue("project", "scene-1")
    response = asyncio.run(handler(DummyRequest(
        match_info={"project_id": "project", "scene_id": "scene-1"},
        body={"base_scene": base, "target_scene": target, "restore_token": token})))
    payload = _response_json(response)

    assert response.status == 200, payload
    assert payload["scene"]["scene_id"] == "scene-1", (
        "a merged document carrying a foreign scene_id was persisted under that "
        "id; the route's re-stamp is what prevents a restore from renaming or "
        "redirecting the scene it restores into")
    assert [scene.scene_id for scene in project.scenes] == ["scene-1"]
    assert payload["scene"]["name"] == "before"
