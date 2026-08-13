"""Server-owned Prompt catalog and atomic MiniMax H3 population tests."""

import copy
from types import SimpleNamespace

import pytest

from server import minimax_h3, prompt_context, routes
from server.project_manager import ProjectVersionConflict
from server.timeline_state import LaneConfig, ReferenceLaneRecipe, Scene, TimelineProject


def _blank_scene():
    return Scene(
        scene_id="scene", reference_lane_count=1,
        reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[ReferenceLaneRecipe(lane_id="blank")])


def _ensure(scene, population="picture"):
    return routes._ensure_minimax_h3_reference_population(
        scene, {"type": "ensure_minimax_h3_reference_population",
                "population": population})


def test_catalog_is_versioned_and_fork_seeds_exclude_server_owned_fields():
    catalog = routes._references_payload(TimelineProject(
        project_id="project"))["prompt_context_catalog"]
    assert catalog["schema_version"] == 1
    assert "role_aliases" not in catalog
    assert "profile_templates" not in catalog
    assert "writing_aids" not in catalog
    assert catalog["profiles"]
    reserved = prompt_context.PROFILE_RESERVED_DEFINITION_FIELDS
    for descriptor in catalog["profiles"]:
        assert not reserved.intersection(descriptor["fork_seed"])
        assert set(descriptor["fork_seed"]).issubset(
            prompt_context.PROFILE_DEFINITION_FIELDS)


def test_custom_profile_identity_is_separate_and_hash_is_server_owned():
    project = TimelineProject(project_id="project")
    seed = prompt_context.profile_fork_seed(
        prompt_context.BUILTIN_PROFILES["generic@1"])
    [created] = routes._create_prompt_context_profile(project, {
        "profile_id": "my_format", "version": "1", "name": "My Format",
        "definition": seed,
    })
    assert created["profile_id"] == "my_format"
    assert created["version"] == "1"
    assert created["name"] == "My Format"
    assert created["content_hash"]
    assert created["content_hash"] != "forged"

    forged = copy.deepcopy(seed)
    forged["content_hash"] = "forged"
    with pytest.raises(routes.ProjectMutationRequestError) as exc:
        routes._create_prompt_context_profile(project, {
            "profile_id": "bad", "version": "1", "name": "Bad",
            "definition": forged,
        })
    assert exc.value.code == "reserved_prompt_context_profile_field"


@pytest.mark.parametrize(("population", "retired_alias"), [
    ("videos", "edit"),
    ("videos", "continue"),
    ("standalone_audios", "reuse"),
    ("standalone_audios", "reference"),
])
def test_retired_role_aliases_remain_visible_but_block_compilation(
        population, retired_alias):
    manifest_key = population
    compiled = prompt_context.compile_prompt_context(
        sections=[], window_start=0, window_end=10,
        template="minimax_h3_ref",
        context={"setup_manifest": {
            "setup": {"mode": "reference"},
            manifest_key: [{"member_id": "saved", "role": retired_alias}],
        }})
    assert any(error["code"] == "unsupported_reference_role"
               and error["member_id"] == "saved"
               for error in compiled["errors"])
    assert retired_alias not in {
        row["value"] for row in
        prompt_context.MINIMAX_H3_ROLE_CATALOGS[population]}


def test_atomic_population_reuses_blank_lane_then_is_semantically_idempotent():
    scene = _blank_scene()
    first = _ensure(scene, "picture")
    state = scene.to_dict()
    second = _ensure(scene, "picture")
    assert first["lane_index"] == 0 and first["materialized"] is True
    assert second["lane_id"] == first["lane_id"]
    assert second["changed"] is False
    assert scene.to_dict() == state


@pytest.mark.parametrize("population,setup_key,physical", [
    ("picture", "picture_lane_ids", "pictures"),
    ("video", "video_lane_ids", "videos"),
    ("audio", "audio_lane_ids", "standalone_audios"),
])
def test_atomic_population_materializes_all_canonical_populations(
        population, setup_key, physical):
    scene = _blank_scene()
    result = _ensure(scene, population)
    setup = minimax_h3.active_setup(scene)
    recipe = scene.reference_lane_recipes[result["lane_index"]]
    assert setup["mode"] == "reference"
    assert setup[setup_key] == [result["lane_id"]]
    assert recipe.recipe["soft"]["physical_population"] == physical


def test_atomic_population_never_rewrites_custom_or_configured_lane():
    custom = ReferenceLaneRecipe(
        lane_id="custom", media_kind="video", recipe_id="custom:owned",
        recipe={"id": "custom:owned", "name": "Authored",
                "hard": {"assembly": "batch"}, "soft": {}})
    scene = Scene(
        scene_id="scene", reference_lane_count=1,
        reference_lane_configs=[LaneConfig(name="Keep", locked=True)],
        reference_lane_recipes=[custom])
    before_recipe = scene.reference_lane_recipes[0].to_dict()
    before_config = scene.reference_lane_configs[0].to_dict()
    result = _ensure(scene, "picture")
    assert result["lane_index"] == 1
    assert scene.reference_lane_recipes[0].to_dict() == before_recipe
    assert scene.reference_lane_configs[0].to_dict() == before_config


def test_exact_lane_owned_by_another_setup_is_not_reused():
    preset = routes._h3_population_preset("pictures")
    owned = routes._canonical_h3_lane_recipe("owned", preset)
    first = minimax_h3.default_reference_setup(picture_lane_ids=["owned"])
    first["setup_id"] = "first"
    active = minimax_h3.default_reference_setup()
    active["setup_id"] = "active"
    scene = Scene(
        scene_id="scene", reference_lane_count=1,
        reference_lane_configs=[LaneConfig(name="Owned")],
        reference_lane_recipes=[owned],
        minimax_h3_conditioning_setups=[first, active],
        active_minimax_h3_setup_id="active")
    result = _ensure(scene, "picture")
    assert result["lane_index"] == 1
    assert minimax_h3.active_setup(scene)["picture_lane_ids"] == [result["lane_id"]]
    assert scene.minimax_h3_conditioning_setups[0]["picture_lane_ids"] == ["owned"]


def test_atomic_population_operation_rejects_embedded_scene_or_extra_fields():
    with pytest.raises(routes.ProjectMutationRequestError) as exc:
        routes._ensure_minimax_h3_reference_population(_blank_scene(), {
            "type": "ensure_minimax_h3_reference_population",
            "population": "picture", "scene_id": "not-body-owned",
        })
    assert exc.value.code == "invalid_h3_reference_population_operation"


def test_stale_scene_mutation_precondition_rolls_back_before_h3_operation():
    scene = _blank_scene()
    project = TimelineProject(project_id="project", scenes=[scene],
                              modified_at="current")
    before = scene.to_dict()
    request = SimpleNamespace(method="POST", headers={"If-Match": "stale"})
    with pytest.raises(ProjectVersionConflict):
        routes._validate_request_project_version(request, project)
        _ensure(scene, "picture")
    assert scene.to_dict() == before
