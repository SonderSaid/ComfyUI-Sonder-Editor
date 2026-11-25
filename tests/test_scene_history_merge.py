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
        "video_lane_configs": [{"lane_id": "v0"}],
        "motion_driver_lane_count": 1,
        "motion_driver_lane_configs": [{"lane_id": "d0"}],
        "audio_lane_count": 1,
        "audio_lane_configs": [{"lane_id": "a0"}],
        "reference_lane_count": 1,
        "reference_lane_configs": [{}],
        "reference_lane_recipes": [{"lane_id": "r0"}],
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
        "linked_item_groups",
    ):
        assert merged[field] == target[field]
    assert merged["minimax_h3_conditioning_setups"] == stored["minimax_h3_conditioning_setups"]
    assert merged["active_minimax_h3_setup_id"] == stored["active_minimax_h3_setup_id"]
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


def _lanes(family, *rows):
    """A lane family's fields: each row an id, or (id, settings)."""
    rows = [(row, {}) if isinstance(row, str) else row for row in rows]
    fields = {f"{family}_lane_count": len(rows)}
    if family == "reference":
        fields["reference_lane_configs"] = [
            {key: value for key, value in settings.items() if key != "recipe"}
            for _, settings in rows]
        fields["reference_lane_recipes"] = [
            {"lane_id": lane_id, "media_kind": "image", **settings.get("recipe", {})}
            for lane_id, settings in rows]
    else:
        fields[f"{family}_lane_configs"] = [
            {"lane_id": lane_id, **settings} for lane_id, settings in rows]
    return fields


def _lane_ids(scene, family):
    key = "reference_lane_recipes" if family == "reference" else f"{family}_lane_configs"
    return [row["lane_id"] for row in scene[key]]


def test_a_lane_changed_since_is_not_taken_away():
    target = _scene(**_lanes("video", "a"))
    base = _scene(**_lanes("video", "a", "b"))
    stored = _scene(**_lanes("video", "a", ("b", {"hidden": True})))

    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, stored)

    assert exc.value.conflicts[0]["path"] == "video_lane_family[b]"


def test_taking_a_lane_away_refuses_to_strand_concurrently_added_clip():
    target = _scene(**_lanes("video", "a"))
    base = _scene(**_lanes("video", "a", "b"),
                  clips=[{"clip_id": "dropped", "track_index": 1}])
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


# -- lanes are merged one by one, by id ------------------------------------

FAMILIES = {
    "video": ("clips", "clip_id", "track_index", {}),
    "motion_driver": ("clips", "clip_id", "track_index", {"role": "motion_driver"}),
    "audio": ("audio_tracks", "track_id", "lane_index", {}),
    "reference": ("reference_items", "reference_item_id", "lane_index", {}),
}
every_family = pytest.mark.parametrize("family", sorted(FAMILIES))


def _items(family, **lanes):
    collection, key, index, extra = FAMILIES[family]
    return {collection: [{key: item, index: lane, **extra} for item, lane in lanes.items()]}


def _where(scene, family):
    """item -> the id of the lane it stands on."""
    collection, key, index, _ = FAMILIES[family]
    ids = _lane_ids(scene, family)
    return {row[key]: ids[row[index]] for row in scene[collection]}


@every_family
def test_a_lane_step_keeps_a_lane_appended_since(family):
    """The measured wedge: a take landed on a new lane after a lane lock, and
    the lock's Undo was refused `video_lane_family` and stuck on top."""
    target = _scene(**_lanes(family, "a", ("b", {"locked": False})))
    base = _scene(**_lanes(family, "a", ("b", {"locked": True})))
    stored = _scene(**_lanes(family, "a", ("b", {"locked": True}), "take"),
                    **_items(family, take=2))
    undone = merge_scene_history(base, target, stored)
    assert undone[f"{family}_lane_configs"][1]["locked"] is False
    assert _lane_ids(undone, family) == ["a", "b", "take"]
    assert _where(undone, family) == {"take": "take"}
    redone = merge_scene_history(target, base, undone)
    assert redone == stored
    # A lost-receipt retry: the first attempt committed, then a lane landed.
    assert merge_scene_history(base, target, undone) == undone


@every_family
def test_a_removed_lane_returns_beside_its_neighbour_not_onto_a_takes_lane(family):
    target = _scene(**_lanes(family, "a", "b", "c"), **_items(family, i=0, m=1, j=2))
    base = _scene(**_lanes(family, "a", "c"), **_items(family, i=0, j=1))
    stored = _scene(**_lanes(family, "a", "c", "take"), **_items(family, i=0, j=1, t=2))
    undone = merge_scene_history(base, target, stored)
    assert _lane_ids(undone, family) == ["a", "b", "c", "take"]
    assert _where(undone, family) == {"i": "a", "m": "b", "j": "c", "t": "take"}
    redone = merge_scene_history(target, base, undone)
    assert _lane_ids(redone, family) == ["a", "c", "take"]
    assert _where(redone, family) == {"i": "a", "j": "c", "t": "take"}


@every_family
def test_an_added_lane_goes_and_comes_back_under_a_take(family):
    target = _scene(**_lanes(family, "a"))
    base = _scene(**_lanes(family, "a", "new"))
    stored = _scene(**_lanes(family, "a", "new", "take"), **_items(family, t=2))
    undone = merge_scene_history(base, target, stored)
    assert _lane_ids(undone, family) == ["a", "take"]
    assert _where(undone, family) == {"t": "take"}
    redone = merge_scene_history(target, base, undone)
    assert _lane_ids(redone, family) == ["a", "new", "take"]
    assert _where(redone, family) == {"t": "take"}


@every_family
def test_an_item_move_follows_its_lane_when_numbers_shifted_since(family):
    """The move kept the lane count; a lane below went and another came."""
    target = _scene(**_lanes(family, "a", "b", "c"), **_items(family, m=0))
    base = _scene(**_lanes(family, "a", "b", "c"), **_items(family, m=2))
    stored = _scene(**_lanes(family, "a", "c", "take"), **_items(family, m=1))
    assert _where(merge_scene_history(base, target, stored), family) == {"m": "a"}


@every_family
def test_a_lane_gone_since_refuses_the_item_the_step_puts_there(family):
    target = _scene(**_lanes(family, "a", "b"), **_items(family, m=1))
    base = _scene(**_lanes(family, "a", "b"), **_items(family, m=0))
    stored = _scene(**_lanes(family, "a"), **_items(family, m=0))
    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, stored)
    assert [item["path"] for item in exc.value.conflicts][0].endswith("_index")


@every_family
def test_settings_merge_one_by_one(family):
    target = _scene(**_lanes(family, ("a", {"locked": False, "name": "x", "future": 1})))
    base = _scene(**_lanes(family, ("a", {"locked": True, "name": "x", "future": 1})))
    other = _scene(**_lanes(family, ("a", {"locked": True, "name": "y", "future": 2}), "take"))
    merged = merge_scene_history(base, target, other)[f"{family}_lane_configs"][0]
    assert (merged["locked"], merged["name"], merged["future"]) == (False, "y", 2)
    clash = _scene(**_lanes(family, ("a", {"locked": "elsewhere", "name": "x"})))
    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, clash)
    assert exc.value.conflicts[0]["path"] == f"{family}_lane_family[a].locked"


def test_one_reference_lanes_recipe_is_one_value():
    def scene(recipe, other="o"):
        return _scene(**_lanes("reference", ("a", {"recipe": {"recipe_id": recipe}}),
                               ("b", {"recipe": {"recipe_id": other}})))
    merged = merge_scene_history(scene("new"), scene("old"), scene("new", other="changed"))
    assert [row["recipe_id"] for row in merged["reference_lane_recipes"]] == ["old", "changed"]
    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(scene("new"), scene("old"), scene("third"))
    assert exc.value.conflicts[0]["path"] == "reference_lane_family[a].recipe"


@every_family
def test_a_family_is_never_left_without_lanes(family):
    target = _scene(**_lanes(family, "a"))
    base = _scene(**_lanes(family, "a", "new"))
    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, _scene(**_lanes(family, "new")))
    assert exc.value.conflicts[0]["path"] == f"{family}_lane_family"


@pytest.mark.parametrize(("stored", "expected"), [
    (("b", "a", "c"), ("a", "b", "c")),                      # nothing since
    (("b", "take", "a", "c"), ("a", "take", "b", "c")),      # a lane since
    (("a", "b", "c"), ("a", "b", "c")),                      # already so
    (("b", "a"), ("a", "b")),                                # a lane went since
    (("c", "a", "b"), None),                                 # reordered since
])
def test_lane_order(stored, expected):
    target = _scene(**_lanes("reference", "a", "b", "c"))
    base = _scene(**_lanes("reference", "b", "a", "c"))
    if expected is None:
        with pytest.raises(SceneMergeConflict) as exc:
            merge_scene_history(base, target, _scene(**_lanes("reference", *stored)))
        assert exc.value.conflicts[0]["path"] == "reference_lane_family.order"
        return
    merged = merge_scene_history(base, target, _scene(**_lanes("reference", *stored)))
    assert tuple(_lane_ids(merged, "reference")) == expected


@pytest.mark.parametrize(("target", "base", "stored", "expected"), [
    (("a", "b", "c"), ("b", "c"), ("b", "c", "t"), ["a", "b", "c", "t"]),   # first lane
    (("a", "b"), ("b",), ("t", "b"), ["t", "a", "b"]),
    (("a", "b", "c", "d"), ("a", "d"), ("a", "d", "t"), ["a", "b", "c", "d", "t"]),
    (("a", "b"), ("a",), ("t",), ["t", "b"]),                                # no neighbour left
])
def test_where_a_returning_lane_goes(target, base, stored, expected):
    merged = merge_scene_history(_scene(**_lanes("video", *base)), _scene(**_lanes("video", *target)),
                                 _scene(**_lanes("video", *stored)))
    assert _lane_ids(merged, "video") == expected


@pytest.mark.parametrize("change", ["kind changed since", "item added since", "neither"])
def test_a_reference_item_never_lands_on_another_media_kind(change):
    def lanes(kind):
        return _lanes("reference", ("a", {"recipe": {"media_kind": kind}}), "b")
    if change == "item added since":
        # Undo of a kind change, over an item placed for the changed kind.
        target, base = _scene(**lanes("image")), _scene(**lanes("audio"))
        stored = _scene(**lanes("audio"), **_items("reference", new=0))
    else:
        # Redo moves the item onto `a`, which meanwhile became an audio lane.
        target = _scene(**lanes("image"), **_items("reference", m=0))
        base = _scene(**lanes("image"), **_items("reference", m=1))
        stored = _scene(**lanes("audio" if change == "kind changed since" else "image"),
                        **_items("reference", m=1))
    if change == "neither":
        assert _where(merge_scene_history(base, target, stored), "reference") == {"m": "a"}
        return
    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, stored)
    assert exc.value.conflicts[0]["path"].endswith(".lane_index.media_kind")


def test_a_role_change_crosses_families_by_lane_id():
    both = {**_lanes("video", "v0", "v1"), **_lanes("motion_driver", "d0")}
    target = _scene(**both, clips=[{"clip_id": "m", "role": "render", "track_index": 1}])
    base = _scene(**_lanes("video", "v0", "v1"), **_lanes("motion_driver", "d0", "d1"),
                  clips=[{"clip_id": "m", "role": "motion_driver", "track_index": 1}])
    stored = _scene(**_lanes("video", "v0", "v1", "t"), **_lanes("motion_driver", "d0", "d1"),
                    clips=base["clips"])
    merged = merge_scene_history(base, target, stored)
    assert merged["clips"] == [{"clip_id": "m", "role": "render", "track_index": 1}]
    assert _lane_ids(merged, "motion_driver") == ["d0"]
    # A concurrent role change leaves the item's lane and role in different families.
    stored["clips"] = [{"clip_id": "m", "role": "render", "track_index": 0}]
    with pytest.raises(SceneMergeConflict):
        merge_scene_history(base, target, stored)


@pytest.mark.parametrize("index", [-1, True, "1", None, 2.0])
def test_an_unreadable_lane_number_is_left_alone(index):
    clip = {"clip_id": "odd", "track_index": index}
    target = _scene(**_lanes("video", "a", "b"), clips=[clip])
    base = _scene(**_lanes("video", "b"), clips=[clip])
    merged = merge_scene_history(base, target, copy.deepcopy(base))
    assert merged["clips"] == [clip] and _lane_ids(merged, "video") == ["a", "b"]


def test_an_unknown_role_keeps_its_number():
    clip = {"clip_id": "odd", "role": "future", "track_index": 1}
    target = _scene(**_lanes("video", "a", "b"), clips=[clip])
    base = _scene(**_lanes("video", "b"), clips=[clip])
    assert merge_scene_history(base, target, copy.deepcopy(base))["clips"] == [clip]


def test_an_item_past_the_last_lane_stays_past_it():
    """Tolerated stored damage: never moved onto a lane a step or a take adds."""
    stranded = {"clip_id": "s", "track_index": 5}
    target = _scene(**_lanes("video", "a", "b"), clips=[stranded])
    base = _scene(**_lanes("video", "a"), clips=[{**stranded, "track_index": 4}])
    merged = merge_scene_history(base, target, copy.deepcopy(base))
    assert merged["clips"][0]["track_index"] == 5
    # One number on both sides, past the last lane on both: only the count moved, so a
    # take that has since covered it does not refuse the step.
    target = _scene(**_lanes("video", "a"), clips=[stranded])
    base = _scene(**_lanes("video", "a", "new"), clips=[stranded])
    stored = _scene(**_lanes("video", "a", "new", "t1", "t2", "t3", "t4"), clips=[stranded])
    merged = merge_scene_history(base, target, stored)
    assert _where(merged, "video") == {"s": "t4"} and "new" not in _lane_ids(merged, "video")


def test_configs_kept_past_the_count_merge_as_one_value():
    def scene(lanes, spare):
        value = _scene(**_lanes("video", *lanes))
        value["video_lane_configs"] += [{"lane_id": lane_id} for lane_id in spare]
        return value
    target, base = scene(("a", "b"), ("s",)), scene(("a",), ())
    merged = merge_scene_history(base, target, scene(("a", "t"), ()))
    assert merged["video_lane_count"] == 3 and _lane_ids(merged, "video") == ["a", "b", "t", "s"]
    # A clash keeps stored's, and one a take has since adopted is a lane, once.
    assert _lane_ids(merge_scene_history(base, target, scene(("a",), ("z",))), "video") == ["a", "b", "z"]
    merged = merge_scene_history(scene(("a", "n"), ("s",)), scene(("a",), ("s",)),
                                 scene(("a", "n", "s"), ()))
    assert _lane_ids(merged, "video") == ["a", "s"] and merged["video_lane_count"] == 2


def test_a_spare_recipe_a_lane_took_over_is_not_kept_beside_it():
    def scene(lanes, spare):
        value = _scene(**_lanes("reference", *lanes))
        value["reference_lane_configs"] += [{} for _ in spare]
        value["reference_lane_recipes"] += [{"lane_id": lane_id} for lane_id in spare]
        return value
    merged = merge_scene_history(scene(("a",), ("c",)), scene(("a", "c"), ()),
                                 scene(("a",), ("c", "d")))
    assert merged["reference_lane_count"] == 2
    assert _lane_ids(merged, "reference") == ["a", "c", "d"]
    assert len(merged["reference_lane_configs"]) == 3


def _without_ids(scene, family):
    scene = copy.deepcopy(scene)
    key = "reference_lane_recipes" if family == "reference" else f"{family}_lane_configs"
    for row in scene[key]:
        row.pop("lane_id")
    return scene


@every_family
@pytest.mark.parametrize("side", ["base", "target", "stored", "duplicate"])
def test_lanes_that_cannot_be_told_apart_refuse_the_step(family, side):
    """A page older than the server, or damaged data. Equal values are not equal lanes:
    undoing a lane removal after a take put the clip on the take's lane."""
    target = _scene(**_lanes(family, "a", "b"), **_items(family, m=1))
    base = _scene(**_lanes(family, "a"))
    stored = _scene(**_lanes(family, "a", "take"), **_items(family, t=1))
    if side == "duplicate":
        stored = _scene(**_lanes(family, "a", "a"), **_items(family, t=1))
    else:
        scenes = {"base": base, "target": target, "stored": stored}
        scenes[side] = _without_ids(scenes[side], family)
        base, target, stored = scenes["base"], scenes["target"], scenes["stored"]
    with pytest.raises(SceneMergeConflict, match="until the editor is reloaded") as exc:
        merge_scene_history(base, target, stored)
    assert exc.value.conflicts[0]["path"] == f"{family}_lane_family"


@every_family
def test_lanes_without_ids_do_not_refuse_a_step_that_leaves_them_alone(family):
    collection, _, index, _ = FAMILIES[family]
    target = _without_ids(_scene(**_lanes(family, "a", "b"), **_items(family, m=0)), family)
    base = copy.deepcopy(target)
    base[collection][0][index] = 1
    assert merge_scene_history(base, target, copy.deepcopy(base))[collection][0][index] == 0
    # A settings step whose result is already stored, on an unchanged count.
    locked = copy.deepcopy(target)
    locked[f"{family}_lane_configs"][0]["locked"] = True
    assert merge_scene_history(locked, target, copy.deepcopy(target)) == target
    with pytest.raises(SceneMergeConflict):
        merge_scene_history(locked, target, copy.deepcopy(locked))


def test_a_missing_family_is_left_alone():
    base = {"clips": [{"clip_id": "c", "track_index": 1}]}
    target = {"clips": [{"clip_id": "c", "track_index": 0}]}
    assert merge_scene_history(base, target, copy.deepcopy(base)) == target


def test_the_inputs_are_never_edited():
    sides = [_scene(**_lanes("video", *ids), **_items("video", m=0))
             for ids in (("a", "n"), ("a",), ("a", "n", "t"))]
    before = copy.deepcopy(sides)
    merge_scene_history(*sides)
    assert sides == before


DOC = {"nodes": [{"type": "text", "node_id": "n", "text": "T"}]}


@pytest.mark.parametrize(("target", "expected"), [
    ({"channels": {"old": "KEPT"}, "channel_docs": {}}, {"old": "KEPT"}),
    ({"channels": {"old": "KEPT"}}, {"old": "KEPT"}),
    # A target older than documents: its text is what the step restores.
    ({"channels": {"old": "KEPT", "new": "WAS"}, "channel_docs": {}},
     {"old": "KEPT", "new": "WAS"}),
    ({"channels": {"old": "KEPT", "new": "WAS"}}, {"old": "KEPT", "new": "WAS"}),
    ({"channels": {"old": "KEPT", "new": "WAS"}, "channel_docs": None},
     {"old": "KEPT", "new": "WAS"}),
])
def test_a_removed_document_leaves_the_text_the_target_holds(target, expected):
    """`old` is text no document holds (written before documents): not a removal."""
    base_row = {"prompt_id": "p", "channels": {"old": "KEPT", "new": "NOW"},
                "channel_docs": {"new": DOC}}
    base = _scene(prompt_sections=[base_row])
    merged = merge_scene_history(
        base, _scene(prompt_sections=[{"prompt_id": "p", **target}]), copy.deepcopy(base))
    assert merged["prompt_sections"][0]["channels"] == expected
    assert base["prompt_sections"][0] == base_row


@pytest.mark.parametrize(("target", "channels", "prompt"), [
    ({"global_channel_docs": {}}, {}, ""),
    ({"global_channel_docs": {}, "prompt": "WAS"}, {}, "WAS"),
    ({"global_channels": {"a": "WAS"}, "prompt": "WAS"}, {"a": "WAS"}, "NOW"),
    ({"global_channels": {"a": "WAS"}, "global_channel_docs": None, "prompt": "WAS"},
     {"a": "WAS"}, "NOW"),
])
def test_a_removed_global_document_leaves_the_text_the_target_holds(target, channels, prompt):
    base = _scene(global_channel_docs={"a": DOC}, global_channels={"a": "NOW"}, prompt="NOW")
    merged = merge_scene_history(base, _scene(**target), copy.deepcopy(base))
    assert merged["global_channels"] == channels and merged["prompt"] == prompt


@pytest.mark.parametrize("sections", ["text", [1], None, 5, True, 2.5, {"a": 1}])
def test_a_malformed_target_is_refused_not_a_crash(sections):
    base = _scene(prompt_sections=[])
    try:
        merge_scene_history(base, _scene(prompt_sections=sections), copy.deepcopy(base))
    except SceneMergeConflict:
        pass


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


# ---------------------------------------------------------------------------
# Every Scene field is classified for history, or the suite fails
# ---------------------------------------------------------------------------
# A field outside the declared write set is silently never restored by Undo, and
# nothing said so: `test_declared_scene_write_set_...` above names its fields by
# hand, so a newly added Scene field is not flagged by it. These dicts close
# that, and they deliberately carry prose -- the class alone does not say why a
# field earns it.
#
# Kept alongside the two hand-written tests above rather than replacing them:
# those are behavioural probes with realistic retimed payloads (the fps case
# asserts the conflict path), while this layer is the exhaustive classification
# that fails when the model grows a field. Two layers, one authority each.

# Compared by the merge so a concurrent edit still conflicts, but never written
# back -- `_merge_value(..., write=False)` in scene_history_merge.
CONFLICT_ONLY_FIELDS = {
    "width": "Inherited geometry: 0 means 'follow the project'. Restoring a "
             "stored value would pin an inherited scene to a literal size.",
    "height": "Inherited geometry, as width.",
}

# Refuses in both directions, even when stored never moved.
TIMEBASE_REFUSE_FIELDS = {
    "fps": "An FPS mutation retimes every frame-based field before returning "
           "its scene, so restoring those fields against a stored FPS corrupts "
           "the timebase. Expiry: revisit when geometry-aware undo exists.",
}

# No scene mutation can write these, so a scene-mutation undo has nothing to
# reverse in them. Asserted below against the real dispatcher allow-list rather
# than trusted, because that is the part that can change under us.
PASS_THROUGH_FIELDS = {
    "order": "Scene position, assigned once by TimelineProject.add_scene. "
             "Nothing reorders scenes, so no mutation can change it.",
    "saved_selections": "Owned by its own REST routes under the scene "
                        "(`/scenes/{id}/saved_selections`), never by a scene "
                        "mutation.",
    # The three below have NO writer at all -- not in `server/`, not in
    # `nodes/`. They round-trip through to_dict/from_dict and nothing else
    # touches them. Saying "owned by X" would be an invention; the honest
    # statement is that they are durable state nobody currently writes, which is
    # a different and more interesting fact than "history does not restore it".
    #
    # Expiry: each leaves this dict when a writer appears -- at which point the
    # question "should Undo restore it?" becomes live and must be answered, not
    # inherited.
    "is_bridge": "No writer in server/ or nodes/; set only in tests and by "
                 "deserialization. Undo has nothing to reverse because nothing "
                 "changes it.",
    "batch_config": "No writer in server/ or nodes/ -- there are no batch "
                    "routes that assign it. Deserialized and re-serialized only.",
    "asset_ids": "No writer in server/ or nodes/. Nothing maintains it today, "
                 "so 'derived membership' would describe an intention rather "
                 "than the code.",
}

# Re-stamped by the restore route from the URL, never merged from a document.
IDENTITY_FIELDS = {
    "scene_id": "Taken from match_info at the raise site; a merged document "
                "must never be able to rename the scene it restores into.",
}


def _scene_field_names():
    import dataclasses

    from server.timeline_state import Scene

    keys = set(Scene(scene_id="probe").to_dict())
    fields = {f.name for f in dataclasses.fields(Scene) if not f.name.startswith("_")}
    assert keys == fields, (
        "Scene.to_dict() and the dataclass fields disagree, so neither is a "
        "trustworthy enumeration: {}".format(keys ^ fields))
    return keys


def _dispatcher_writable_scene_fields():
    """Input keys `_apply_scene_fields` membership-tests directly.

    Deliberately narrow, and narrower than "fields a scene mutation can write":
    it reads `"x" in fields` tests in one function. It does not follow the
    helpers that function calls (`_set_scene_lane_count`, `retime_scene_geometry`,
    `_ensure_scene_lane_config_lengths`, `_require_scene_duration_within_cap`),
    and it does not look at the other 37 dispatcher branches at all.

    That is enough for the one claim it supports -- that the pass-through five are
    not in this allow-list -- and not enough for the stronger claim that nothing
    writes them, which is asserted separately by having checked every assignment
    in `server/` and `nodes/` by hand and recorded the result in the dict's prose.

    `descriptor.count_attr in fields` is resolved through VARIABLE_LANE_DESCRIPTORS
    because a constant-only scan reports 12 when the real list is 16, and the four
    it would miss are exactly the lane counts a new lane family adds.
    """
    import ast
    from pathlib import Path

    from server.lane_registry import VARIABLE_LANE_DESCRIPTORS

    source = Path(__file__).resolve().parents[1] / "server" / "routes.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(
        (node for node in ast.walk(tree)
         if isinstance(node, ast.FunctionDef) and node.name == "_apply_scene_fields"),
        None)
    assert function is not None, "_apply_scene_fields not found; the scan is blind"

    writable, unresolved = set(), []
    for node in ast.walk(function):
        if not (isinstance(node, ast.Compare)
                and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)):
            continue
        if any(isinstance(op, ast.NotIn) for op in node.ops):
            # A negative membership test reads a key just as much as a positive
            # one; treating it as invisible rather than unresolved is how a
            # scan silently understates the surface it is asserting about.
            unresolved.append(f"negative membership: {ast.unparse(node)}")
            continue
        if not any(isinstance(c, ast.Name) and c.id == "fields"
                   for c in node.comparators):
            continue
        if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str):
            writable.add(node.left.value)
        elif ast.unparse(node.left) == "descriptor.count_attr":
            writable |= {d.count_attr for d in VARIABLE_LANE_DESCRIPTORS}
        else:
            unresolved.append(ast.unparse(node.left))
    assert not unresolved, (
        "a membership test this scan cannot resolve makes the pass-through "
        "classification unprovable; resolve it explicitly: {}".format(unresolved))
    return writable


def test_every_scene_field_has_a_history_classification():
    from server.scene_history_merge import (
        MERGED_DERIVED_FIELDS, scene_history_write_fields)

    classified = {}
    for name, group in (("write", scene_history_write_fields()),
                        ("derived", MERGED_DERIVED_FIELDS),
                        ("conflict-only", CONFLICT_ONLY_FIELDS),
                        ("timebase-refuse", TIMEBASE_REFUSE_FIELDS),
                        ("pass-through", PASS_THROUGH_FIELDS),
                        ("identity", IDENTITY_FIELDS)):
        for field in group:
            assert field not in classified, (
                "{} is claimed by both {} and {}".format(field, classified[field], name))
            classified[field] = name

    fields = _scene_field_names()
    assert not fields - set(classified), (
        "a new Scene field is not classified for history, so Undo's behaviour "
        "for it is undeclared. Add it to the write set if Undo should restore "
        "it, or to one of the dicts above with a reason: {}".format(
            sorted(fields - set(classified))))
    assert not set(classified) - fields, (
        "these are classified but are no longer Scene fields, so the entry has "
        "rotted: {}".format(sorted(set(classified) - fields)))


def test_pass_through_fields_are_unreachable_from_scene_mutations():
    """The pass-through rationale, machine-checked rather than narrated."""
    writable = _dispatcher_writable_scene_fields()
    reachable = writable & set(PASS_THROUGH_FIELDS)
    assert not reachable, (
        "these are classified pass-through -- 'no scene mutation can change "
        "them, so an undo has nothing to reverse' -- but update_scene_fields "
        "now writes them, so the classification is false: {}".format(sorted(reachable)))
    assert set(CONFLICT_ONLY_FIELDS) | set(TIMEBASE_REFUSE_FIELDS) <= writable, (
        "geometry and timebase are classified as conflict-only *because* a "
        "mutation can write them; if it no longer can, they are pass-through")


@pytest.mark.parametrize("field,base_value,target_value", [
    ("width", 1280, 640),
    ("height", 720, 480),
    ("fps", 48.0, 24.0),
    ("order", 2, 1),
    ("is_bridge", True, False),
    ("asset_ids", ["b"], ["a"]),
    ("scene_id", "scene", "other"),
])
def test_unwritten_classes_never_restore_their_field(field, base_value, target_value):
    """Column 1 of the class table: base != target, stored == base.

    Everything outside the write set keeps stored. `fps` is the exception that
    makes it a class of its own -- it refuses here rather than passing through.
    """
    base = _scene(**{field: base_value})
    target = _scene(**{field: target_value})
    stored = copy.deepcopy(base)

    if field in TIMEBASE_REFUSE_FIELDS:
        with pytest.raises(SceneMergeConflict):
            merge_scene_history(base, target, stored)
        return
    assert merge_scene_history(base, target, stored)[field] == base_value


@pytest.mark.parametrize("field,expect_conflict", [
    ("width", True), ("height", True), ("fps", True),
    ("order", False), ("is_bridge", False), ("asset_ids", False),
])
def test_column_two_separates_compared_fields_from_pass_through(field, expect_conflict):
    """Column 2: base != target and stored moved too.

    This is the cell that tells a compared field from an ignored one. Without it
    conflict-only and pass-through are indistinguishable, because both keep
    stored whenever stored did not move.
    """
    values = {"width": (1280, 640, 1920), "height": (720, 480, 1080),
              "fps": (48.0, 24.0, 30.0), "order": (2, 1, 3),
              "is_bridge": (True, False, True), "asset_ids": (["b"], ["a"], ["c"])}
    base_value, target_value, stored_value = values[field]
    base = _scene(**{field: base_value})
    target = _scene(**{field: target_value})
    stored = _scene(**{field: stored_value})

    if expect_conflict:
        with pytest.raises(SceneMergeConflict):
            merge_scene_history(base, target, stored)
    else:
        assert merge_scene_history(base, target, stored)[field] == stored_value


def test_derived_fields_do_not_survive_the_persistence_boundary():
    """What separates `derived` from `pass-through`.

    Under the merge alone the two are indistinguishable -- both keep stored and
    neither conflicts -- so classifying by merge behaviour would let them be
    collapsed with no visible change.

    Derived from the dicts rather than hardcoded, because an earlier version
    named its two fields inline and therefore could not fail when a pass-through
    field was misclassified as derived. It also states the boundary per field:
    `prompt` is recomputed on the way OUT (`Scene.to_dict`), while
    `global_channels` is rebuilt on the way IN (`Scene.__post_init__`). An
    earlier attempt asserted the save-side rule for both and was simply wrong
    about `global_channels`, which `to_dict` writes verbatim.
    """
    from server.scene_history_merge import MERGED_DERIVED_FIELDS
    from server.timeline_state import BatchConfig, Scene

    assert set(MERGED_DERIVED_FIELDS) == {"global_channels", "prompt"}, (
        "the derived class changed; this test asserts a per-field boundary and "
        "a new member needs its own")

    # `prompt`: recomputed at to_dict, so a stale direct assignment never leaves.
    scene = Scene(scene_id="scene")
    scene.set_global_prompt("authored text")
    scene.prompt = "stale direct assignment"
    assert scene.to_dict()["prompt"] == "authored text"

    # `global_channels`: rebuilt at from_dict from the documents, so a stale
    # value does not survive a round trip even though to_dict copies it.
    document = scene.to_dict()
    document["global_channels"] = {"visual": "stale"}
    assert Scene.from_dict(document).to_dict()["global_channels"] != {"visual": "stale"}

    # A pass-through field, by contrast, round-trips unchanged: persistence may
    # normalise its shape once (saved_selections gains its default keys), but it
    # is never recomputed from somewhere else the way a derived field is. That
    # is what makes the two classes different, and why collapsing them would
    # change behaviour even though the merge treats them identically.
    held = {"order": 7, "is_bridge": True, "batch_config": BatchConfig(max_frames=8),
            "saved_selections": [{"name": "keep"}], "asset_ids": ["a"]}
    assert set(held) == set(PASS_THROUGH_FIELDS), (
        "a pass-through field has no round-trip value here, so nothing proves it "
        f"survives: {sorted(set(PASS_THROUGH_FIELDS) ^ set(held))}")
    for field, value in held.items():
        setattr(scene, field, value)
    once = scene.to_dict()
    twice = Scene.from_dict(once).to_dict()
    for field in PASS_THROUGH_FIELDS:
        assert twice[field] == once[field], field
    # And the values really are the ones set, not defaults that would pass above.
    assert once["order"] == 7 and once["is_bridge"] is True
    assert once["batch_config"]["max_frames"] == 8
    assert once["asset_ids"] == ["a"]
    assert once["saved_selections"][0]["name"] == "keep"


# One probe value triple per declared write field. A field declared writable with
# no entry here fails: `write` is the cheapest class and the default a new field
# drifts into, and it was the one class with a partition but no predicate. A
# field can be listed in MERGED_WRITE_FIELDS without the merge actually writing
# it, and the browser paints an optimistic Undo for anything in that list
# (`_historyOptimisticEligibility` treats `writable` membership as paintable), so
# a wrong entry paints a change the server will never apply.
def _lane_probe(family, field):
    """A lane family is written together, so its probe carries the whole family."""
    base, target = _lanes(family, "x", "y"), _lanes(family, "x")
    return base[field], target[field], base, target


_WRITE_FIELD_PROBES = {
    "name": ("after", "before"),
    "duration_frames": (48, 24),
    "generation_params": ({"seed": 2}, {"seed": 1}),
    "prompt_context_profile_id": ("after", "before"),
    "prompt_context_profile_config": ({"mode": "after"}, {"mode": "before"}),
    "guide_track_config": ({"hidden": True}, {"hidden": False}),
    "prompt_track_config": ({"hidden": True}, {"hidden": False}),
    "global_prompt_track_config": ({"hidden": True}, {"hidden": False}),
    "global_channel_docs": ({"main": {"text": "after"}}, {"main": {"text": "before"}}),
    "clips": ([{"clip_id": "c", "timeline_start_frame": 8}],
              [{"clip_id": "c", "timeline_start_frame": 4}]),
    "audio_tracks": ([{"track_id": "a", "timeline_start_frame": 8}],
                     [{"track_id": "a", "timeline_start_frame": 4}]),
    "guide_frames": ([{"guide_id": "g", "frame_index": 8}],
                     [{"guide_id": "g", "frame_index": 4}]),
    "reference_items": ([{"reference_item_id": "r", "start_frame": 8}],
                        [{"reference_item_id": "r", "start_frame": 4}]),
    "prompt_sections": ([{"prompt_id": "p", "start_frame": 8, "end_frame": 12}],
                        [{"prompt_id": "p", "start_frame": 4, "end_frame": 12}]),
    "linked_item_groups": ([{"group_id": "g", "items": [2, 3]}],
                           [{"group_id": "g", "items": [1, 2]}]),
    "global_attachments": ([{"attachment_id": "x", "role": "after"}],
                           [{"attachment_id": "x", "role": "before"}]),
    "video_lane_count": _lane_probe("video", "video_lane_count"),
    "video_lane_configs": _lane_probe("video", "video_lane_configs"),
    "motion_driver_lane_count": _lane_probe("motion_driver", "motion_driver_lane_count"),
    "motion_driver_lane_configs": _lane_probe("motion_driver", "motion_driver_lane_configs"),
    "audio_lane_count": _lane_probe("audio", "audio_lane_count"),
    "audio_lane_configs": _lane_probe("audio", "audio_lane_configs"),
    "reference_lane_count": _lane_probe("reference", "reference_lane_count"),
    "reference_lane_configs": _lane_probe("reference", "reference_lane_configs"),
    "reference_lane_recipes": _lane_probe("reference", "reference_lane_recipes"),
}


def test_every_declared_write_field_has_a_probe():
    """`write` is the only class the partition cannot check on its own.

    Membership comes from `scene_history_write_fields()` itself, so the
    classification test is circular for it: whatever is declared is, by
    definition, in the class. This is the decision-forcing point for that class.
    """
    from server.scene_history_merge import scene_history_write_fields

    declared = set(scene_history_write_fields())
    missing = sorted(declared - set(_WRITE_FIELD_PROBES))
    assert not missing, (
        "these are declared writable by history but nothing proves the merge can "
        f"actually write them. Add a (base, target) probe pair: {missing}")
    stale = sorted(set(_WRITE_FIELD_PROBES) - declared)
    assert not stale, f"probes for fields no longer declared writable: {stale}"


@pytest.mark.parametrize("field", sorted(_WRITE_FIELD_PROBES))
def test_a_declared_write_field_is_actually_written(field):
    """Column 1 for the write class: base != target, stored == base -> target."""
    base_value, target_value, *family = _WRITE_FIELD_PROBES[field]
    base = _scene(**(family[0] if family else {field: base_value}))
    target = _scene(**(family[1] if family else {field: target_value}))
    merged = merge_scene_history(base, target, copy.deepcopy(base))
    assert merged[field] == target_value, (
        f"{field} is declared in MERGED_WRITE_FIELDS but the merge did not "
        "write it. The browser paints an optimistic Undo for anything in that "
        "list, so a field declared here but unwritten paints a change the "
        "server will never apply")
