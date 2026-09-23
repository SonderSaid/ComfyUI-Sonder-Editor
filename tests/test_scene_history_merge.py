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


# -- 0.6.0 L4d: lanes appended outside history (take placement) -------------

def _lock_step(count_field, configs_field, *, count=2, lane=0):
    """(target, base): a step that locked one lane and kept the count."""
    configs = [{"name": f"lane {index}", "locked": False} for index in range(count)]
    target = _scene(**{count_field: count, configs_field: configs})
    locked = copy.deepcopy(configs)
    locked[lane]["locked"] = True
    base = _scene(**{count_field: count, configs_field: locked})
    return target, base


def _append_lane(scene, count_field, configs_field, extra=None):
    """What `ensure_lane_index` / `_append_generated_lanes` do: count + 1 and a
    default config padded at the end, plus the take's item on the new lane."""
    appended = copy.deepcopy(scene)
    appended[count_field] += 1
    appended[configs_field].append({})
    for key, value in (extra or {}).items():
        appended[key] = appended.get(key, []) + [value]
    return appended


@pytest.mark.parametrize(("count_field", "configs_field", "item"), [
    ("video_lane_count", "video_lane_configs",
     ("clips", {"clip_id": "take", "track_index": 2})),
    ("audio_lane_count", "audio_lane_configs",
     ("audio_tracks", {"track_id": "take", "lane_index": 2})),
    ("motion_driver_lane_count", "motion_driver_lane_configs", None),
])
def test_undo_of_a_lane_step_keeps_a_lane_appended_since(count_field, configs_field, item):
    """The measured wedge: a take landed on a new lane after a lane lock, and
    the lock's Undo was refused `video_lane_family` and stuck on top."""
    target, base = _lock_step(count_field, configs_field)
    stored = _append_lane(base, count_field, configs_field,
                          dict([item]) if item else None)

    merged = merge_scene_history(base, target, stored)

    assert merged[count_field] == 3, "the appended lane is kept"
    assert merged[configs_field] == [*target[configs_field], {}], (
        "the step is reverted on the existing lanes only")
    if item:
        assert merged[item[0]] == [item[1]], "the take on the new lane is kept"


def test_redo_of_a_lane_step_keeps_a_lane_appended_since():
    target, base = _lock_step("video_lane_count", "video_lane_configs")
    # Redo applies the step forward: base is the pre-step scene, target the post.
    stored = _append_lane(target, "video_lane_count", "video_lane_configs")

    merged = merge_scene_history(target, base, stored)

    assert merged["video_lane_count"] == 3
    assert merged["video_lane_configs"] == [*base["video_lane_configs"], {}]


def test_a_reference_recipe_step_keeps_an_appended_reference_lane():
    target = _scene(reference_lane_count=1, reference_lane_configs=[{}],
                    reference_lane_recipes=[{"lane_id": "a", "recipe_id": "old"}])
    base = _scene(reference_lane_count=1, reference_lane_configs=[{}],
                  reference_lane_recipes=[{"lane_id": "a", "recipe_id": "new"}])
    stored = copy.deepcopy(base)
    stored["reference_lane_count"] = 2
    stored["reference_lane_configs"].append({})
    stored["reference_lane_recipes"].append({"lane_id": "b", "recipe_id": ""})

    merged = merge_scene_history(base, target, stored)

    assert merged["reference_lane_count"] == 2
    assert merged["reference_lane_recipes"] == [
        {"lane_id": "a", "recipe_id": "old"}, {"lane_id": "b", "recipe_id": ""}]


def test_a_retry_after_an_append_finds_the_step_already_applied():
    """A lost-receipt retry: the first attempt committed, then a lane landed."""
    target, base = _lock_step("video_lane_count", "video_lane_configs")
    stored = _append_lane(target, "video_lane_count", "video_lane_configs")

    assert merge_scene_history(base, target, stored) == stored


@pytest.mark.parametrize("change", [
    "existing_lane_edited", "step_changed_the_count", "lane_removed_then_added",
    "stored_shorter"])
def test_an_append_does_not_excuse_any_other_lane_change(change):
    """Everything but a pure tail append still refuses as one unit -- for lanes
    whose configs tell them apart. `_lock_step` names every lane; with default
    configs the older by-value shortcut can alias instead (see the xfail below).
    """
    target, base = _lock_step("video_lane_count", "video_lane_configs", count=3)
    stored = _append_lane(base, "video_lane_count", "video_lane_configs")
    if change == "existing_lane_edited":
        stored["video_lane_configs"][1]["hidden"] = True
    elif change == "step_changed_the_count":
        # Undo of "add lane" after a take landed: removing the step's lane
        # would shift the take's lane under its clip.
        target = copy.deepcopy(base)
        target["video_lane_count"] = 2
        target["video_lane_configs"] = target["video_lane_configs"][:2]
    elif change == "lane_removed_then_added":
        del stored["video_lane_configs"][1]
        stored["video_lane_configs"].append({"name": "new"})
    elif change == "stored_shorter":
        stored["video_lane_configs"] = stored["video_lane_configs"][:2]

    with pytest.raises(SceneMergeConflict) as exc:
        merge_scene_history(base, target, stored)

    assert exc.value.conflicts[0]["path"] == "video_lane_family"


@pytest.mark.xfail(strict=True, reason=(
    "pre-existing, bug tracker: with default lane configs, undoing a step that "
    "removed a lane after a take was appended at the same index finds the "
    "family equal to target BY VALUE, takes the no-op shortcut, and restores "
    "the clip onto the take's lane. Flips to a failure when that is fixed."))
def test_undo_of_a_lane_removal_after_a_take_refuses_instead_of_aliasing():
    default = {}
    # The step: deleting the lone clip on lane 1 compacted that lane away.
    target = _scene(video_lane_count=2, video_lane_configs=[default, default],
                    clips=[{"clip_id": "c", "track_index": 1}])
    base = _scene(video_lane_count=1, video_lane_configs=[default])
    # Then a take landed on a new lane -- index 1 again.
    stored = _scene(video_lane_count=2, video_lane_configs=[default, default],
                    clips=[{"clip_id": "take", "track_index": 1}])

    with pytest.raises(SceneMergeConflict):
        merge_scene_history(base, target, stored)


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
    it reads `"x" in fields` tests in one function. It does not follow the four
    helpers that function calls (`_set_scene_lane_count`, `retime_scene_geometry`,
    `_clamp_reference_items_to_scene`, `_ensure_scene_lane_config_lengths`), and
    it does not look at the other 37 dispatcher branches at all.

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
    "video_lane_count": (2, 1),
    "video_lane_configs": ([{}, {}], [{}]),
    "motion_driver_lane_count": (2, 1),
    "motion_driver_lane_configs": ([{}, {}], [{}]),
    "audio_lane_count": (2, 1),
    "audio_lane_configs": ([{}, {}], [{}]),
    "reference_lane_count": (2, 1),
    "reference_lane_configs": ([{}, {}], [{}]),
    "reference_lane_recipes": ([{"a": 1}], [{}]),
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
    base_value, target_value = _WRITE_FIELD_PROBES[field]
    base = _scene(**{field: base_value})
    target = _scene(**{field: target_value})
    merged = merge_scene_history(base, target, copy.deepcopy(base))
    assert merged[field] == target_value, (
        f"{field} is declared in MERGED_WRITE_FIELDS but the merge did not "
        "write it. The browser paints an optimistic Undo for anything in that "
        "list, so a field declared here but unwritten paints a change the "
        "server will never apply")
