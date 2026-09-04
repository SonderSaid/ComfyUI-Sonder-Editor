import copy

import pytest

from server.scene_history_merge import (
    SceneMergeConflict,
    SceneRestoreReceiptStore,
    merge_scene_history,
)


def _scene(**changes):
    value = {
        "scene_id": "scene",
        "name": "Scene",
        "duration_frames": 24,
        "clips": [],
        "audio_tracks": [],
        "guide_frames": [],
        "prompt_sections": [],
        "reference_items": [],
        "linked_item_groups": [],
        "global_channel_docs": {},
        "global_attachments": [],
        "minimax_h3_conditioning_setups": [],
        "video_lane_count": 1,
        "video_lane_configs": [{}],
        "motion_driver_lane_count": 1,
        "motion_driver_lane_configs": [{}],
        "audio_lane_count": 1,
        "audio_lane_configs": [{}],
        "reference_lane_count": 1,
        "reference_lane_configs": [{}],
        "reference_lane_recipes": [{}],
    }
    value.update(copy.deepcopy(changes))
    return value


def test_merge_reverses_owned_field_and_preserves_concurrent_new_member():
    target = _scene(clips=[{"clip_id": "clip", "timeline_start_frame": 0,
                            "timeline_end_frame": 8, "takes": []}])
    base = copy.deepcopy(target)
    base["clips"][0]["timeline_start_frame"] = 4
    stored = copy.deepcopy(base)
    stored["clips"][0]["takes"].append({"asset_id": "take"})
    stored["clips"].append({"clip_id": "generated", "timeline_start_frame": 8,
                             "timeline_end_frame": 16})

    merged = merge_scene_history(base, target, stored)

    assert merged["clips"][0]["timeline_start_frame"] == 0
    assert merged["clips"][0]["takes"] == [{"asset_id": "take"}]
    assert merged["clips"][1]["clip_id"] == "generated"


def test_merge_conflicts_when_both_sides_change_same_field():
    target = _scene(clips=[{"clip_id": "clip", "timeline_start_frame": 0}])
    base = _scene(clips=[{"clip_id": "clip", "timeline_start_frame": 4}])
    stored = _scene(clips=[{"clip_id": "clip", "timeline_start_frame": 7}])

    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, stored)

    assert exc.value.conflicts[0]["path"] == "clips[clip].timeline_start_frame"


def test_merge_is_idempotent_when_the_same_patch_was_already_committed():
    target = _scene(
        name="Before",
        clips=[{"clip_id": "clip", "timeline_start_frame": 0,
                "timeline_end_frame": 8}],
        global_attachments=[],
    )
    base = _scene(
        name="After",
        clips=[{"clip_id": "clip", "timeline_start_frame": 4,
                "timeline_end_frame": 12},
               {"clip_id": "added", "timeline_start_frame": 12,
                "timeline_end_frame": 20}],
        global_attachments=[{"attachment_id": "added"}],
    )
    stored = copy.deepcopy(target)
    stored["clips"].append({
        "clip_id": "concurrent", "timeline_start_frame": 16,
        "timeline_end_frame": 24})

    merged = merge_scene_history(base, target, stored)

    assert merged == stored


def test_merge_preserves_absence_instead_of_writing_null():
    target = _scene(clips=[{"clip_id": "clip", "timeline_start_frame": 0}])
    base = _scene(clips=[{"clip_id": "clip", "timeline_start_frame": 4,
                          "takes": [{"asset_id": "take"}]}])
    stored = copy.deepcopy(base)

    merged = merge_scene_history(base, target, stored)

    assert "takes" not in merged["clips"][0]


def test_derived_prompt_mirrors_do_not_conflict_across_channel_edits():
    target = _scene(global_channel_docs={"visual": {"text": "before"}},
                    prompt="before", global_channels={"visual": "before"})
    base = _scene(global_channel_docs={"visual": {"text": "after"}},
                  prompt="after", global_channels={"visual": "after"})
    stored = copy.deepcopy(base)
    stored["global_channel_docs"]["speech"] = {"text": "concurrent"}
    stored["prompt"] = "after concurrent"
    stored["global_channels"] = {"visual": "after", "speech": "concurrent"}

    merged = merge_scene_history(base, target, stored)

    assert merged["global_channel_docs"] == {
        "visual": {"text": "before"}, "speech": {"text": "concurrent"}}
    assert merged["prompt"] == "after concurrent"
    assert merged["global_channels"]["speech"] == "concurrent"


def test_section_channel_documents_merge_per_channel_without_mirror_conflict():
    target_section = {
        "prompt_id": "prompt", "start_frame": 0, "end_frame": 8,
        "channel_docs": {"visual": {"text": "before"}},
        "channels": {"visual": "before"}, "prompt": "before",
    }
    base_section = copy.deepcopy(target_section)
    base_section["channel_docs"]["visual"] = {"text": "after"}
    base_section["channels"]["visual"] = "after"
    base_section["prompt"] = "after"
    target = _scene(prompt_sections=[target_section])
    base = _scene(prompt_sections=[base_section])
    stored = copy.deepcopy(base)
    stored["prompt_sections"][0]["channel_docs"]["speech"] = {
        "text": "concurrent"}
    stored["prompt_sections"][0]["channels"]["speech"] = "concurrent"
    stored["prompt_sections"][0]["prompt"] = "after concurrent"

    merged = merge_scene_history(base, target, stored)

    section = merged["prompt_sections"][0]
    assert section["channel_docs"] == {
        "visual": {"text": "before"}, "speech": {"text": "concurrent"}}
    assert section["prompt"] == "after concurrent"
    assert section["channels"]["speech"] == "concurrent"


def test_declared_scene_write_set_restores_and_out_of_scope_fields_survive():
    target = _scene(
        name="Before", generation_params={"seed": 1},
        prompt_context_profile_id="profile-before",
        prompt_context_profile_config={"mode": "before"},
        global_prompt_track_config={"hidden": False},
        guide_track_config={"hidden": False},
        prompt_track_config={"hidden": False},
        linked_item_groups=[{"group_id": "group", "items": [1, 2]}],
        minimax_h3_conditioning_setups=[{"setup_id": "setup", "name": "Before"}],
        active_minimax_h3_setup_id="setup-before",
        batch_config={"max_frames": 8}, saved_selections=[{"name": "keep"}],
        asset_ids=["asset"], order=7, is_bridge=True,
    )
    base = copy.deepcopy(target)
    base.update({
        "name": "After", "generation_params": {"seed": 2},
        "prompt_context_profile_id": "profile-after",
        "prompt_context_profile_config": {"mode": "after"},
        "global_prompt_track_config": {"hidden": True},
        "guide_track_config": {"hidden": True},
        "prompt_track_config": {"hidden": True},
        "active_minimax_h3_setup_id": "setup-after",
        "linked_item_groups": [{"group_id": "group", "items": [2, 3]}],
        "minimax_h3_conditioning_setups": [
            {"setup_id": "setup", "name": "After"}],
    })
    stored = copy.deepcopy(base)
    stored["saved_selections"].append({"name": "concurrent"})
    stored["asset_ids"].append("concurrent")

    merged = merge_scene_history(base, target, stored)

    for field in (
        "name", "generation_params", "prompt_context_profile_id",
        "prompt_context_profile_config", "global_prompt_track_config",
        "guide_track_config", "prompt_track_config",
        "active_minimax_h3_setup_id",
        "linked_item_groups", "minimax_h3_conditioning_setups",
    ):
        assert merged[field] == target[field]
    assert merged["saved_selections"] == [
        {"name": "keep"}, {"name": "concurrent"}]
    assert merged["asset_ids"] == ["asset", "concurrent"]
    assert merged["batch_config"] == {"max_frames": 8}
    assert merged["order"] == 7
    assert merged["is_bridge"] is True


def test_plain_recursive_equality_accepts_integral_float_drift():
    target = _scene(generation_params={"nested": [1]})
    base = _scene(generation_params={"nested": [2]})
    stored = _scene(generation_params={"nested": [2.0]})

    assert merge_scene_history(base, target, stored)["generation_params"] == {
        "nested": [1]}


def test_lane_family_merges_atomically_and_conflicts_as_one_unit():
    target = _scene(video_lane_count=1, video_lane_configs=[{"name": "one"}])
    base = _scene(video_lane_count=2,
                  video_lane_configs=[{"name": "one"}, {"name": "two"}])
    stored = copy.deepcopy(base)
    stored["video_lane_configs"][1]["hidden"] = True

    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, stored)

    assert exc.value.conflicts[0]["path"] == "video_lane_family"


def test_lane_shrink_refuses_to_strand_concurrently_added_clip():
    target = _scene(video_lane_count=1,
                    video_lane_configs=[{"name": "one"}])
    base = _scene(
        video_lane_count=2,
        video_lane_configs=[{"name": "one"}, {"name": "drop lane"}],
        clips=[{"clip_id": "dropped", "track_index": 1}],
    )
    stored = copy.deepcopy(base)
    stored["clips"].append({
        "clip_id": "concurrent", "track_index": 1,
        "timeline_start_frame": 12, "timeline_end_frame": 20,
    })

    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, stored)

    assert exc.value.conflicts[0]["path"] == "clips[concurrent].track_index"


def test_geometry_is_conflict_only_and_never_written():
    target = _scene(width=640)
    base = _scene(width=1280)
    stored = copy.deepcopy(base)
    assert merge_scene_history(base, target, stored)["width"] == 1280
    stored["width"] = 1920
    with pytest.raises(SceneMergeConflict):
        merge_scene_history(base, target, stored)

    fps_target = _scene(fps=24, duration_frames=24,
                        clips=[{"clip_id": "clip", "timeline_start_frame": 4,
                                "timeline_end_frame": 12}])
    fps_base = _scene(fps=48, duration_frames=48,
                      clips=[{"clip_id": "clip", "timeline_start_frame": 8,
                              "timeline_end_frame": 24}])
    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(fps_base, fps_target, copy.deepcopy(fps_base))
    assert exc.value.conflicts[0]["path"] == "fps"


def test_duration_change_refuses_concurrently_added_to_end_reference():
    target = _scene(duration_frames=12)
    base = _scene(duration_frames=24)
    stored = copy.deepcopy(base)
    stored["reference_items"].append({
        "reference_item_id": "concurrent", "start_frame": 8, "end_frame": -1})

    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, stored)

    assert exc.value.conflicts[-1]["path"].endswith("effective_end_frame")


def test_idempotent_duration_retry_preserves_new_to_end_reference():
    target = _scene(duration_frames=12)
    base = _scene(duration_frames=24)
    stored = copy.deepcopy(target)
    stored["reference_items"].append({
        "reference_item_id": "concurrent", "start_frame": 8, "end_frame": -1})

    merged = merge_scene_history(base, target, stored)

    assert merged == stored


@pytest.mark.parametrize(("field", "key", "owned_field"), [
    ("audio_tracks", "track_id", "volume"),
    ("guide_frames", "guide_id", "strength"),
    ("reference_items", "reference_item_id", "strength"),
])
def test_keyed_item_fields_revert_without_erasing_concurrent_fields(
        field, key, owned_field):
    target_member = {key: "item", owned_field: 0.25, "muted": False}
    base_member = {**target_member, owned_field: 0.75}
    stored_member = {**base_member, "muted": True}
    target = _scene(**{field: [target_member]})
    base = _scene(**{field: [base_member]})
    stored = _scene(**{field: [stored_member]})

    merged = merge_scene_history(base, target, stored)[field][0]

    assert merged[owned_field] == 0.25
    assert merged["muted"] is True


@pytest.mark.parametrize(("field", "key"), [
    ("linked_item_groups", "group_id"),
    ("global_attachments", "attachment_id"),
    ("minimax_h3_conditioning_setups", "setup_id"),
])
def test_whole_value_members_revert_and_preserve_concurrent_new_members(field, key):
    target = _scene(**{field: [{key: "owned", "value": "before"}]})
    base = _scene(**{field: [{key: "owned", "value": "after"}]})
    stored = copy.deepcopy(base)
    stored[field].append({key: "concurrent", "value": "keep"})

    merged = merge_scene_history(base, target, stored)[field]

    assert merged == [
        {key: "owned", "value": "before"},
        {key: "concurrent", "value": "keep"},
    ]


@pytest.mark.parametrize(("path", "keys"), [
    ("motion_driver_lane_family",
     ("motion_driver_lane_count", "motion_driver_lane_configs")),
    ("audio_lane_family", ("audio_lane_count", "audio_lane_configs")),
    ("reference_lane_family", (
        "reference_lane_count", "reference_lane_configs", "reference_lane_recipes")),
])
def test_each_lane_family_uses_one_atomic_conflict_unit(path, keys):
    target = _scene()
    base = _scene()
    stored = _scene()
    target[keys[0]] = 1
    base[keys[0]] = 2
    stored[keys[0]] = 2
    target[keys[1]] = [{"name": "one"}]
    base[keys[1]] = [{"name": "one"}, {"name": "two"}]
    stored[keys[1]] = copy.deepcopy(base[keys[1]])
    stored[keys[1]][1]["hidden"] = True
    if len(keys) == 3:
        target[keys[2]] = [{"mode": "one"}]
        base[keys[2]] = [{"mode": "one"}, {"mode": "two"}]
        stored[keys[2]] = copy.deepcopy(base[keys[2]])

    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, stored)

    assert exc.value.conflicts[0]["path"] == path


def test_restore_receipts_are_count_bounded_and_ttl_prunes_every_entry(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr("server.scene_history_merge.time.monotonic",
                        lambda: clock["now"])
    store = SceneRestoreReceiptStore(max_entries=2, ttl_seconds=10)
    first = store.issue("project", "scene")
    second = store.issue("project", "scene")
    assert store.get(first, "project", "scene") is not None
    third = store.issue("project", "scene")

    assert store.get(second, "project", "scene") is None
    assert store.get(first, "project", "scene") is not None
    assert store.get(third, "project", "scene") is not None

    clock["now"] = 111.0
    assert store.get(first, "project", "scene") is None
    assert store.get(third, "project", "scene") is None
