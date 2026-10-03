"""A Reference lane recipe edit is a per-field patch (backlog step 1, Phase 5).

[#62]: every recipe field edit used to send the WHOLE painted recipe, and so did
a lane lock or rename. A write queued behind a failed one therefore re-sent the
failed field as part of what its author saw, and the server stored the edit the
author had just been told was lost.

`update_lane_config` now accepts `fields.reference_recipe_patch`, a
`{hard?, soft?}` map of the keys one edit writes, guarded by
`expected.reference_recipe_fields`, the prior value of each patched key
(`{"$absent": true}` for a key the stored recipe lacks). A mismatched prior is
refused `409 identity_mismatch`, which is what stops a second edit of the same
key from re-committing a failed one. The merged recipe goes through the same
`ReferenceLaneRecipe.from_dict` as a whole-value write and its values are not
validated more strictly, so the stored value equals the editor's paint.
"""

import copy
import json

import pytest

from server import routes
from server.timeline_state import (
    LaneConfig,
    ReferenceLaneRecipe,
    Scene,
    TimelineProject,
)

ABSENT = {"$absent": True}


def _project(recipe=None, *, recipe_id="custom:mine", media_kind="image"):
    recipe = copy.deepcopy(recipe if recipe is not None else {
        "name": "Mine",
        "hard": {"assembly": "batch", "max_members": 4, "frame_step": 8,
                 "frame_grid_source": "model", "frame_rate": 24.0},
        "soft": {"prompt_prefix": "lead"},
    })
    scene = Scene(
        scene_id="scene-1", duration_frames=100,
        reference_lane_count=1,
        reference_lane_configs=[LaneConfig(name="Lane")],
        reference_lane_recipes=[ReferenceLaneRecipe(
            lane_id="lane-a", media_kind=media_kind, recipe_id=recipe_id,
            recipe=recipe)])
    project = TimelineProject(project_id="p", scenes=[scene], reference_recipes=[
        {"id": "custom:mine", "name": "Mine", "media_kind": "image",
         "hard": {}, "soft": {}}])
    return TimelineProject.from_dict(json.loads(json.dumps(project.to_dict())))


def _patch_op(patch, priors, *, lane_id="lane-a", lane_type="reference", extra_fields=None,
              extra_expected=None):
    expected = {"reference_recipe_fields": priors}
    if lane_id is not None:
        expected["lane_id"] = lane_id
    expected.update(extra_expected or {})
    return {"type": "update_lane_config", "lane_type": lane_type, "lane_index": 0,
            "fields": {"reference_recipe_patch": patch, **(extra_fields or {})},
            "expected": expected}


def _batch(project, operations):
    return routes._apply_scene_mutation_batch(project, "scene-1", operations)


def _refusal(project, operations):
    before = copy.deepcopy(project.get_scene("scene-1").to_dict())
    with pytest.raises(routes.ProjectMutationRequestError) as caught:
        _batch(project, operations)
    after = project.get_scene("scene-1").to_dict()
    return caught.value, before == after


def _lane(project):
    return project.get_scene("scene-1").to_dict()["reference_lane_recipes"][0]


# -- the patch -----------------------------------------------------------------------

def test_a_patch_writes_only_the_keys_it_names():
    project = _project()
    committed, payload = _batch(project, [_patch_op(
        {"hard": {"max_members": 5}}, {"hard": {"max_members": 4}})])
    lane = _lane(project)
    assert committed is True
    assert lane["recipe"]["hard"] == {"assembly": "batch", "max_members": 5, "frame_step": 8,
                                      "frame_grid_source": "model", "frame_rate": 24.0}
    assert lane["recipe"]["soft"] == {"prompt_prefix": "lead"}
    assert lane["recipe"]["name"] == "Mine"
    assert (lane["lane_id"], lane["recipe_id"], lane["media_kind"]) == (
        "lane-a", "custom:mine", "image")
    # The lane's config is not part of a patch.
    assert project.get_scene("scene-1").reference_lane_configs[0].name == "Lane"
    # The positive acknowledgement an editor checks: a server that ignores the
    # field cannot produce it.
    assert payload["results"][0]["reference_recipe_patch"] is True


def test_a_patch_writes_hard_and_soft_together():
    project = _project()
    _batch(project, [_patch_op(
        {"hard": {"assembly": "sheet"}, "soft": {"prompt_prefix": "new"}},
        {"hard": {"assembly": "batch"}, "soft": {"prompt_prefix": "lead"}})])
    lane = _lane(project)
    assert lane["recipe"]["hard"]["assembly"] == "sheet"
    assert lane["recipe"]["soft"]["prompt_prefix"] == "new"


def test_a_key_the_recipe_lacks_is_guarded_by_the_absent_marker():
    project = _project()
    _batch(project, [_patch_op({"soft": {"prompt_suffix": "tail"}},
                               {"soft": {"prompt_suffix": ABSENT}})])
    assert _lane(project)["recipe"]["soft"]["prompt_suffix"] == "tail"
    # A section the recipe lacks entirely is absent too.
    bare = _project({"name": "Bare"})
    _batch(bare, [_patch_op({"hard": {"max_members": 2}},
                            {"hard": {"max_members": ABSENT}})])
    assert _lane(bare)["recipe"]["hard"] == {"max_members": 2}


def test_an_absent_prior_for_a_stored_key_is_refused():
    project = _project()
    error, unchanged = _refusal(project, [_patch_op(
        {"hard": {"max_members": 5}}, {"hard": {"max_members": ABSENT}})])
    assert (error.status, error.code) == (409, "identity_mismatch")
    assert unchanged


def test_a_pegged_value_carries_its_source():
    project = _project()
    _batch(project, [_patch_op(
        {"hard": {"frame_step": 9, "frame_grid_source": "custom"}},
        {"hard": {"frame_step": 8, "frame_grid_source": "model"}})])
    hard = _lane(project)["recipe"]["hard"]
    assert (hard["frame_step"], hard["frame_grid_source"]) == (9, "custom")


def test_a_changed_prior_is_refused_and_nothing_in_the_patch_is_written():
    """The same-key 409: a second edit of a key whose first edit failed."""
    project = _project()
    error, unchanged = _refusal(project, [_patch_op(
        {"hard": {"max_members": 6, "frame_step": 9}},
        {"hard": {"max_members": 5, "frame_step": 8}})])
    assert (error.status, error.code) == (409, "identity_mismatch")
    assert error.message == "This recipe setting changed before your edit saved."
    assert unchanged


def test_a_same_key_patch_queued_behind_an_accepted_one_applies_in_order():
    project = _project()
    _batch(project, [
        _patch_op({"hard": {"max_members": 5}}, {"hard": {"max_members": 4}}),
        _patch_op({"hard": {"max_members": 6}}, {"hard": {"max_members": 5}}),
    ])
    assert _lane(project)["recipe"]["hard"]["max_members"] == 6


def test_priors_compare_numbers_by_value_and_never_a_boolean_as_a_number():
    # JSON from the editor spells a stored 24.0 as 24.
    project = _project()
    _batch(project, [_patch_op({"hard": {"frame_rate": 30}},
                               {"hard": {"frame_rate": 24}})])
    assert _lane(project)["recipe"]["hard"]["frame_rate"] == 30
    flagged = _project({"name": "F", "hard": {"max_members": 1}})
    error, unchanged = _refusal(flagged, [_patch_op(
        {"hard": {"max_members": 2}}, {"hard": {"max_members": True}})])
    assert error.code == "identity_mismatch" and unchanged


def test_patched_values_are_stored_as_sent():
    """No stricter than a whole-value write, so the stored value is the paint."""
    project = _project()
    _batch(project, [_patch_op(
        {"hard": {"max_members": 999, "assembly": "not-a-mode"},
         "soft": {"suggested_tags": ["x", "y"]}},
        {"hard": {"max_members": 4, "assembly": "batch"},
         "soft": {"suggested_tags": ABSENT}})])
    recipe = _lane(project)["recipe"]
    assert recipe["hard"]["max_members"] == 999
    assert recipe["hard"]["assembly"] == "not-a-mode"
    assert recipe["soft"]["suggested_tags"] == ["x", "y"]


def test_a_patch_round_trips_through_a_save():
    project = _project()
    _batch(project, [_patch_op({"hard": {"max_members": 5}}, {"hard": {"max_members": 4}})])
    saved = TimelineProject.from_dict(json.loads(json.dumps(project.to_dict())))
    assert saved.get_scene("scene-1").reference_lane_recipes[0].recipe["hard"]["max_members"] == 5


# -- refusals ------------------------------------------------------------------------

@pytest.mark.parametrize("patch,priors", [
    ({"hard": {"no_such_key": 1}}, {"hard": {"no_such_key": ABSENT}}),
    # `prompt_prefix` is declared in `soft`.
    ({"hard": {"prompt_prefix": "x"}}, {"hard": {"prompt_prefix": ABSENT}}),
    ({"bogus": {"max_members": 1}}, {"bogus": {"max_members": 4}}),
    ({"hard": []}, {"hard": []}),
    ([], {}),
    ({}, {}),
    ({"hard": {}}, {"hard": {}}),
])
def test_a_malformed_patch_is_refused(patch, priors):
    project = _project()
    error, unchanged = _refusal(project, [_patch_op(patch, priors)])
    assert (error.status, error.code) == (400, "invalid_reference_recipe")
    assert unchanged


@pytest.mark.parametrize("priors", [
    None,
    {},
    {"hard": {}},
    {"hard": {"max_members": 4, "frame_step": 8}},
    {"hard": {"max_members": 4}, "soft": {"prompt_prefix": "lead"}},
    {"hard": "x"},
])
def test_the_priors_must_name_exactly_the_patched_keys(priors):
    project = _project()
    operation = _patch_op({"hard": {"max_members": 5}}, priors)
    if priors is None:
        del operation["expected"]["reference_recipe_fields"]
    error, unchanged = _refusal(project, [operation])
    assert (error.status, error.code) == (400, "invalid_reference_recipe")
    assert unchanged


def test_a_patch_beside_a_whole_recipe_is_refused():
    project = _project()
    error, unchanged = _refusal(project, [_patch_op(
        {"hard": {"max_members": 5}}, {"hard": {"max_members": 4}},
        extra_fields={"reference_recipe": _lane(project)})])
    assert (error.status, error.code) == (400, "invalid_reference_recipe")
    assert unchanged


def test_a_patch_needs_the_lane_identity():
    project = _project()
    error, unchanged = _refusal(project, [_patch_op(
        {"hard": {"max_members": 5}}, {"hard": {"max_members": 4}}, lane_id=None)])
    assert (error.status, error.code) == (409, "identity_mismatch")
    assert unchanged


def test_a_patch_on_a_lane_without_a_recipe_is_refused():
    project = _project()
    error, unchanged = _refusal(project, [_patch_op(
        {"hard": {"max_members": 5}}, {"hard": {"max_members": 4}},
        lane_type="video", lane_id=None)])
    assert (error.status, error.code) == (400, "invalid_reference_recipe")
    assert unchanged


def test_a_section_stored_as_a_non_object_is_not_patched():
    """Preserved at rest by tolerant loading; a patch must not replace it."""
    project = _project({"name": "Odd", "hard": "kept as stored"})
    error, unchanged = _refusal(project, [_patch_op(
        {"hard": {"max_members": 2}}, {"hard": {"max_members": ABSENT}})])
    assert (error.status, error.code) == (409, "reference_recipe_unpatchable")
    assert unchanged
    assert _lane(project)["recipe"]["hard"] == "kept as stored"


# -- lock and rename without the recipe ---------------------------------------------

def test_a_config_write_without_a_recipe_leaves_the_recipe_and_lane_id_untouched():
    project = _project()
    before = copy.deepcopy(_lane(project))
    _batch(project, [{"type": "update_lane_config", "lane_type": "reference",
                      "lane_index": 0, "expected": {"lane_id": "lane-a"},
                      "fields": {"name": "Renamed", "color": "", "locked": True,
                                 "hidden": False}}])
    assert _lane(project) == before
    config = project.get_scene("scene-1").reference_lane_configs[0]
    assert (config.name, config.locked) == ("Renamed", True)


# -- support ------------------------------------------------------------------------

def test_the_library_payload_advertises_recipe_patches():
    assert routes._references_payload(_project())["lane_recipe_patch"] is True


# -- the editor's paint is the stored recipe (mirror parity) -------------------------

_PARITY_CASES = [
    ("a stored key", {"name": "R", "hard": {"max_members": 4, "assembly": "batch"},
                      "soft": {"prompt_prefix": "a"}},
     {"hard": {"max_members": 5}}),
    ("a key the section lacks", {"name": "R", "soft": {"prompt_prefix": "a"}},
     {"soft": {"prompt_suffix": "z"}}),
    ("a section the recipe lacks", {"name": "R"}, {"hard": {"max_members": 2}}),
    ("an empty recipe", {}, {"soft": {"suggested_tags": ["x"]}}),
    ("a peg pair and a list", {"hard": {"frame_step": 8, "frame_grid_source": "model",
                                        "live_outputs": ["image_slots"]}},
     {"hard": {"frame_step": 9, "frame_grid_source": "custom",
               "live_outputs": ["image_slots", "reference_names"]}}),
    ("both sections", {"hard": {"assembly": "batch"}, "soft": {}},
     {"hard": {"assembly": "sheet"}, "soft": {"prompt_prefix": "p"}}),
    ("a null section", {"hard": None}, {"hard": {"max_members": 1}}),
    ("a list section", {"soft": ["kept"]}, {"soft": {"prompt_prefix": "p"}}),
    ("a text section", {"hard": "kept"}, {"hard": {"max_members": 1}}),
]


def test_the_editor_paints_exactly_what_the_server_stores():
    """`laneRecipePatchPlan` mirrors `_patched_reference_lane_recipe`.

    The decision in both directions -- a section the server refuses to patch is
    one the editor writes whole -- and, where it patches, the priors the editor
    sends are the ones the server accepts and its paint is the recipe stored.
    """
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the mirror parity check")
    module = (Path(__file__).resolve().parents[1] / "web/js/reference_lane_identity.js").as_uri()
    lanes = [{"lane_id": "lane-a", "media_kind": "image", "recipe_id": "custom:mine",
              "recipe": recipe} for _, recipe, _ in _PARITY_CASES]
    patches = [patch for _, _, patch in _PARITY_CASES]
    script = (f"import {{ laneRecipePatchPlan }} from {json.dumps(module)};\n"
              f"const lanes = {json.dumps(lanes)};\nconst patches = {json.dumps(patches)};\n"
              "console.log(JSON.stringify(lanes.map((lane, i) => laneRecipePatchPlan(lane, patches[i]))));\n")
    completed = subprocess.run([node, "--input-type=module", "-e", script],
                               capture_output=True, text=True, encoding="utf-8")
    assert completed.returncode == 0, completed.stderr
    plans = json.loads(completed.stdout)
    for (name, recipe, patch), plan in zip(_PARITY_CASES, plans):
        project = _project(recipe)
        operation = _patch_op(plan["patch"], plan["priors"])
        if not plan["patchable"]:
            error, unchanged = _refusal(project, [operation])
            assert error.code == "reference_recipe_unpatchable" and unchanged, name
            continue
        _batch(project, [operation])
        assert _lane(project)["recipe"] == plan["recipe"]["recipe"], name
