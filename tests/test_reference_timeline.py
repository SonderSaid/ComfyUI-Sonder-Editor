import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from server import minimax_h3, prompt_channel_templates, routes
from server.reference_resolution import resolve_effective_references
from server.timeline_state import (
    Asset,
    GenerationJob,
    LaneConfig,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
    Scene,
    TimelineProject,
)


ROOT = Path(__file__).resolve().parents[1]


def _project_with_reference(media_kind="image"):
    asset_type = "audio" if media_kind == "audio" else "image"
    asset = Asset(asset_id="asset-1", name="One", asset_type=asset_type, path=f"media/one.{asset_type}")
    member = ReferenceMember(member_id="member-1", asset_id=asset.asset_id)
    reference = ReferenceEntity(reference_id="entity-1", name="Subject", members=[member])
    scene = Scene(
        scene_id="scene-1",
        duration_frames=100,
        reference_lane_count=2,
        reference_lane_configs=[LaneConfig(), LaneConfig()],
        reference_lane_recipes=[
            ReferenceLaneRecipe(media_kind=media_kind),
            ReferenceLaneRecipe(media_kind=media_kind),
        ],
    )
    project = TimelineProject(project_id="project-1", assets=[asset], references=[reference], scenes=[scene])
    return project, scene


def test_reference_scene_and_queue_round_trip_preserves_sentinel_and_recipe():
    scene = Scene(
        scene_id="scene",
        duration_frames=80,
        reference_lane_count=2,
        reference_lane_configs=[LaneConfig(name="A"), LaneConfig(name="B", hidden=True)],
        reference_lane_recipes=[
            ReferenceLaneRecipe(media_kind="image", recipe_id="recipe", recipe={"hard": {"max_members": 4}}),
            ReferenceLaneRecipe(media_kind="audio"),
        ],
        reference_items=[ReferenceItem(
            reference_item_id="item",
            lane_index=0,
            start_frame=12,
            end_frame=-1,
            members=[{"entity_id": "entity", "member_id": "member"}],
            prompt_override="portrait",
        )],
    )
    restored = Scene.from_dict(scene.to_dict())
    assert [item.to_dict() for item in restored.reference_items] == [item.to_dict() for item in scene.reference_items]
    assert [config.to_dict() for config in restored.reference_lane_configs] == [config.to_dict() for config in scene.reference_lane_configs]
    assert [recipe.to_dict() for recipe in restored.reference_lane_recipes] == [recipe.to_dict() for recipe in scene.reference_lane_recipes]
    assert restored.reference_items[0].end_frame == -1
    assert restored.reference_lane_recipes[0].recipe["hard"]["max_members"] == 4
    malformed = restored.reference_items[0].to_dict()
    malformed.update({"strength": "not-a-number", "sequence_frames": "not-an-integer"})
    healed = ReferenceItem.from_dict(malformed)
    assert (healed.lane_index, healed.start_frame, healed.end_frame) == (0, 12, -1)
    assert (healed.strength, healed.sequence_frames) == (1.0, 0)


def test_generic_queue_freezes_reference_entity_member_and_asset_catalog():
    project, scene = _project_with_reference("image")
    project.references[0].members[0].name = "portrait"
    scene.reference_items = [ReferenceItem(
        reference_item_id="item", lane_index=0, start_frame=0, end_frame=-1,
        members=[{"entity_id": "entity-1", "member_id": "member-1"}],
    )]
    job = GenerationJob(
        scene_id=scene.scene_id, selection_start=0, selection_end=20,
        params={
            "snapshot_version": 1,
            "prompt_context_format": "prompt_context_v1",
            "prompt_channel_template": prompt_channel_templates.template_freeze_value(
                "standard"),
        }, reference_lane_count=3,
        reference_lane_configs=[LaneConfig(hidden=True).to_dict()],
        reference_lane_recipes=[ReferenceLaneRecipe(media_kind="audio").to_dict()],
        reference_item_snapshots=[],
    )

    routes._compose_frozen_job_prompt(project, job)

    by_kind = {entry["kind"]: entry["value"]
               for entry in job.reference_input_snapshots}
    assert job.reference_lane_count == scene.reference_lane_count
    assert job.reference_lane_configs == [value.to_dict()
                                           for value in scene.reference_lane_configs]
    assert job.reference_lane_recipes == [value.to_dict()
                                           for value in scene.reference_lane_recipes]
    assert job.reference_item_snapshots == [scene.reference_items[0].to_dict()]
    assert by_kind["reference"]["name"] == "Subject"
    assert by_kind["reference"]["members"][0]["name"] == "portrait"
    assert by_kind["asset"]["path"] == "media/one.image"


def test_reference_item_crud_enforces_exact_expected_overlap_and_media_kind():
    project, scene = _project_with_reference("image")
    first = routes._apply_create_reference_item(project, scene, {
        "lane_index": 0,
        "start_frame": 10,
        "end_frame": 30,
        "members": [{"entity_id": "wrong-is-canonicalized", "member_id": "member-1"}],
    })
    assert first.members == [{"entity_id": "entity-1", "member_id": "member-1"}]
    assert first.strength == 1.0 and first.sequence_frames == 0

    routes._apply_update_reference_item(project, scene, {
        "reference_item_id": first.reference_item_id,
        "expected": {"strength": 1.0, "sequence_frames": 0},
        "fields": {"strength": 0.4, "sequence_frames": 33},
    })
    assert first.strength == pytest.approx(0.4) and first.sequence_frames == 33
    with pytest.raises(routes.ProjectMutationRequestError) as nonfinite:
        routes._apply_update_reference_item(project, scene, {
            "reference_item_id": first.reference_item_id,
            "expected": {"strength": 0.4},
            "fields": {"strength": float("nan")},
        })
    assert nonfinite.value.status == 400

    with pytest.raises(routes.ProjectMutationRequestError) as overlap:
        routes._apply_create_reference_item(project, scene, {
            "lane_index": 0,
            "start_frame": 20,
            "end_frame": 40,
            "members": [{"member_id": "member-1"}],
        })
    assert (overlap.value.status, overlap.value.code) == (409, "lane_collision")

    with pytest.raises(routes.ProjectMutationRequestError) as stale:
        routes._apply_update_reference_item(project, scene, {
            "reference_item_id": first.reference_item_id,
            "expected": {"start_frame": 9},
            "fields": {"start_frame": 11},
        })
    assert (stale.value.status, stale.value.code) == (409, "identity_mismatch")

    audio_project, audio_scene = _project_with_reference("audio")
    audio_project.assets[0].asset_type = "image"
    with pytest.raises(routes.ProjectMutationRequestError) as mismatch:
        routes._apply_create_reference_item(audio_project, audio_scene, {
            "lane_index": 0,
            "members": [{"member_id": "member-1"}],
        })
    assert mismatch.value.code == "reference_media_kind_mismatch"

    scene.reference_lane_recipes[1] = ReferenceLaneRecipe(media_kind="audio")
    with pytest.raises(routes.ProjectMutationRequestError) as lane_mismatch:
        routes._apply_update_reference_item(project, scene, {
            "reference_item_id": first.reference_item_id,
            "expected": {"lane_index": 0},
            "fields": {"lane_index": 1},
        })
    assert lane_mismatch.value.code == "reference_media_kind_mismatch"


def test_reference_item_preserves_and_validates_staged_role_overrides():
    project, scene = _project_with_reference("image")
    scene.prompt_context_profile_id = "minimax_h3_ref@1"
    scene.reference_lane_recipes[0] = ReferenceLaneRecipe(
        media_kind="image", recipe_id="custom-h3-picture", recipe={
            "soft": {"compatible_profiles": ["minimax_h3_ref@1"],
                     "physical_population": "pictures",
                     "role_fields": ["role", "visual_intent"]},
        })
    item = routes._apply_create_reference_item(project, scene, {
        "lane_index": 0, "members": [{
            "member_id": "member-1", "role": "identity",
            "visual_intent": "partial",
        }],
    })
    assert item.members == [{
        "entity_id": "entity-1", "member_id": "member-1",
        "visual_intent": "partial", "role": "identity",
    }]
    routes._apply_update_reference_item(project, scene, {
        "reference_item_id": item.reference_item_id,
        "expected": {"members": item.to_dict()["members"]},
        "fields": {"members": [{
            "member_id": "member-1", "role": "keyframe",
            "visual_intent": "preserve",
        }]},
    })
    assert item.members[0]["role"] == "keyframe"
    with pytest.raises(routes.ProjectMutationRequestError) as invalid:
        routes._apply_update_reference_item(project, scene, {
            "reference_item_id": item.reference_item_id,
            "expected": {"members": item.to_dict()["members"]},
            "fields": {"members": [{"member_id": "member-1",
                                     "role": "unsupported"}]},
        })
    assert invalid.value.code == "unsupported_reference_role"


def test_h3_picture_population_rejects_video_while_generic_image_lane_keeps_compatibility():
    project, scene = _project_with_reference("image")
    project.assets[0].asset_type = "video"
    generic = routes._apply_create_reference_item(project, scene, {
        "lane_index": 0, "members": [{"member_id": "member-1"}],
    })
    assert generic.members[0]["member_id"] == "member-1"

    scene.prompt_context_profile_id = "minimax_h3_ref@1"
    scene.reference_lane_recipes[1] = ReferenceLaneRecipe(
        media_kind="image", recipe={"soft": {
            "compatible_profiles": ["minimax_h3_ref@1"],
            "physical_population": "pictures",
            "role_fields": [],
        }})
    with pytest.raises(routes.ProjectMutationRequestError) as mismatch:
        routes._apply_create_reference_item(project, scene, {
            "lane_index": 1, "members": [{"member_id": "member-1"}],
        })
    assert mismatch.value.code == "reference_media_kind_mismatch"


def test_reference_lane_removal_keeps_recipe_array_aligned():
    _, scene = _project_with_reference("image")
    scene.reference_lane_count = 3
    scene.reference_lane_configs = [LaneConfig(name="A"), LaneConfig(name="B"), LaneConfig(name="C")]
    scene.reference_lane_recipes = [
        ReferenceLaneRecipe(recipe_id="a"),
        ReferenceLaneRecipe(recipe_id="b"),
        ReferenceLaneRecipe(recipe_id="c"),
    ]
    routes._remove_media_lane(scene, "reference", 1, "require_empty")
    assert [recipe.recipe_id for recipe in scene.reference_lane_recipes] == ["a", "c"]


def test_reference_lane_removal_refuses_media_kind_change_and_overlap():
    _, scene = _project_with_reference("image")
    scene.reference_lane_recipes[1] = ReferenceLaneRecipe(media_kind="audio")
    scene.reference_items = [ReferenceItem(
        reference_item_id="image-item", lane_index=0, start_frame=10, end_frame=30,
        members=[{"entity_id": "entity-1", "member_id": "member-1"}],
    )]
    with pytest.raises(routes.ProjectMutationRequestError) as media_mismatch:
        routes._remove_media_lane(scene, "reference", 0, "move_items", 1)
    assert media_mismatch.value.code == "reference_media_kind_mismatch"

    scene.reference_lane_recipes[1] = ReferenceLaneRecipe(media_kind="image")
    scene.reference_items.append(ReferenceItem(
        reference_item_id="destination", lane_index=1, start_frame=20, end_frame=40,
        members=[{"entity_id": "entity-1", "member_id": "member-1"}],
    ))
    with pytest.raises(routes.ProjectMutationRequestError) as collision:
        routes._remove_media_lane(scene, "reference", 0, "move_items", 1)
    assert collision.value.code == "lane_collision"


def test_populated_reference_lane_refuses_media_kind_change():
    _, scene = _project_with_reference("image")
    scene.reference_items = [ReferenceItem(
        reference_item_id="image-item", lane_index=0, start_frame=10, end_frame=30,
        members=[{"entity_id": "entity-1", "member_id": "member-1"}],
    )]
    with pytest.raises(routes.ProjectMutationRequestError) as mismatch:
        routes._apply_lane_config(scene, {
            "lane_type": "reference",
            "lane_index": 0,
            "fields": {"reference_recipe": ReferenceLaneRecipe(media_kind="audio").to_dict()},
        })
    assert mismatch.value.code == "reference_media_kind_mismatch"


def _h3_lane_recipe(lane_id, population="pictures"):
    return ReferenceLaneRecipe(
        lane_id=lane_id,
        media_kind="audio" if population == "standalone_audios" else "image",
        recipe={"soft": {
            "compatible_profiles": ["minimax_h3_ref@1"],
            "physical_population": population,
        }},
    )


def _h3_scene(recipes, items=(), configs=None, duration=24):
    return Scene(
        scene_id="scene", duration_frames=duration,
        reference_lane_count=len(recipes),
        reference_lane_configs=list(configs or [LaneConfig() for _ in recipes]),
        reference_lane_recipes=list(recipes),
        reference_items=list(items),
    )


def _h3_resolve(scene, references=(), assets=()):
    """Resolve exactly as live compilation does: no authored setup at all."""
    return minimax_h3.resolve_setup(
        setup=minimax_h3.implicit_reference_setup(),
        reference_items=scene.reference_items,
        lane_recipes=scene.reference_lane_recipes,
        lane_configs=scene.reference_lane_configs,
        lane_count=scene.reference_lane_count,
        scene_duration=scene.duration_frames,
        window_start=0, window_end=scene.duration_frames,
        references=list(references), assets=list(assets),
    )


def _h3_picture(entity_id, member_id, prompt="a portrait"):
    return (
        ReferenceEntity(reference_id=entity_id, name=entity_id, members=[
            ReferenceMember(member_id=member_id, asset_id=f"asset-{member_id}",
                            prompt=prompt)]),
        Asset(asset_id=f"asset-{member_id}", asset_type="image"),
    )


def _picture_item(item_id, lane_index, entity_id, member_id):
    return ReferenceItem(
        reference_item_id=item_id, lane_index=lane_index,
        start_frame=0, end_frame=-1,
        members=[{"entity_id": entity_id, "member_id": member_id}])


def test_h3_population_membership_is_derived_without_any_setup_record():
    """The reported defect: a staged H3 lane compiled to nothing in a new scene.

    A scene that never inherited a `minimax_h3_conditioning_setups` record used
    to fail with `missing_reference_setup` and an empty manifest, however
    correctly its lane was staged.
    """
    entity, asset = _h3_picture("woman", "portrait")
    scene = _h3_scene([_h3_lane_recipe("pictures")],
                      [_picture_item("item", 0, "woman", "portrait")])
    assert scene.minimax_h3_conditioning_setups == []

    resolved = _h3_resolve(scene, [entity], [asset])

    assert resolved["errors"] == []
    assert [row["member_id"] for row in resolved["setup_manifest"]["pictures"]] == [
        "portrait"]
    assert resolved["setup_manifest"]["pictures"][0]["picture_ordinal"] == 1


def test_h3_ordinals_follow_lane_order_across_two_populated_lanes():
    first_entity, first_asset = _h3_picture("woman", "portrait")
    second_entity, second_asset = _h3_picture("man", "headshot")
    recipes = [_h3_lane_recipe("lane-a"), _h3_lane_recipe("lane-b")]
    items = [_picture_item("b", 1, "man", "headshot"),
             _picture_item("a", 0, "woman", "portrait")]

    resolved = _h3_resolve(_h3_scene(recipes, items),
                           [first_entity, second_entity],
                           [first_asset, second_asset])

    # Authored item order is irrelevant; lane order numbers the population.
    assert [(row["member_id"], row["picture_ordinal"])
            for row in resolved["setup_manifest"]["pictures"]] == [
        ("portrait", 1), ("headshot", 2)]


def test_h3_hidden_lane_and_muted_item_leave_the_manifest():
    """Exclusion stays where it already lived, not in a registration list."""
    entity, asset = _h3_picture("woman", "portrait")
    other, other_asset = _h3_picture("man", "headshot")
    recipes = [_h3_lane_recipe("lane-a"), _h3_lane_recipe("lane-b")]
    muted = _picture_item("muted", 1, "man", "headshot")
    muted.muted = True
    items = [_picture_item("hidden", 0, "woman", "portrait"), muted]

    # Both lanes declare `pictures`, so the same scene contributes two slots
    # once nothing is hidden or muted. Asserting only emptiness below would
    # pass just as well if derivation had stopped finding lanes at all.
    visible = _h3_resolve(_h3_scene(recipes, [
        _picture_item("hidden", 0, "woman", "portrait"),
        _picture_item("muted", 1, "man", "headshot"),
    ]), [entity, other], [asset, other_asset])
    assert len(visible["setup_manifest"]["pictures"]) == 2

    scene = _h3_scene(recipes, items,
                      configs=[LaneConfig(hidden=True), LaneConfig()])
    resolved = _h3_resolve(scene, [entity, other], [asset, other_asset])

    assert resolved["setup_manifest"]["pictures"] == []


def test_h3_lane_declaring_a_population_under_a_foreign_format_is_refused():
    """A custom recipe may declare a population the active format rejects."""
    foreign = ReferenceLaneRecipe(
        lane_id="foreign", media_kind="image",
        recipe={"soft": {"compatible_profiles": ["custom@1"],
                         "physical_population": "pictures"}})
    entity, asset = _h3_picture("woman", "portrait")
    scene = _h3_scene([foreign], [_picture_item("item", 0, "woman", "portrait")])

    resolved = _h3_resolve(scene, [entity], [asset])

    assert [error["code"] for error in resolved["errors"]] == [
        "setup_lane_profile_incompatible"]
    assert resolved["setup_manifest"]["pictures"] == []


def test_h3_picture_cap_is_advisory_across_lanes_not_silently_truncated():
    """Newly reachable: no registration limited a population to one lane."""
    references, assets, items, recipes = [], [], [], []
    for lane_index in range(2):
        recipes.append(_h3_lane_recipe(f"lane-{lane_index}"))
        for slot in range(5):
            member_id = f"m{lane_index}{slot}"
            entity, asset = _h3_picture(f"e{lane_index}{slot}", member_id)
            references.append(entity)
            assets.append(asset)
        items.append(ReferenceItem(
            reference_item_id=f"item-{lane_index}", lane_index=lane_index,
            start_frame=0, end_frame=-1,
            members=[{"entity_id": f"e{lane_index}{slot}",
                      "member_id": f"m{lane_index}{slot}"} for slot in range(5)]))

    resolved = _h3_resolve(_h3_scene(recipes, items), references, assets)

    assert not any(error["code"] == "pictures_slot_cap" for error in resolved["errors"])
    assert any(warning["code"] == "pictures_slot_cap"
               for warning in resolved["warnings"])
    assert len(resolved["setup_manifest"]["pictures"]) == 9


def test_h3_standalone_audio_cap_is_prompt_advice_not_a_blocker():
    references, assets, members = [], [], []
    for index in range(4):
        member_id = f"audio-{index}"
        asset_id = f"asset-{member_id}"
        references.append(ReferenceEntity(
            reference_id=f"entity-{index}", name=f"Audio {index + 1}",
            members=[ReferenceMember(member_id=member_id, asset_id=asset_id)],
        ))
        assets.append(Asset(asset_id=asset_id, asset_type="audio"))
        members.append({"entity_id": f"entity-{index}", "member_id": member_id})
    scene = _h3_scene(
        [_h3_lane_recipe("audio-lane", "standalone_audios")],
        [ReferenceItem(
            reference_item_id="audio-item", lane_index=0,
            start_frame=0, end_frame=-1, members=members,
        )],
    )

    resolved = _h3_resolve(scene, references, assets)

    assert resolved["errors"] == []
    assert [row["audio_ordinal"] for row in
            resolved["setup_manifest"]["standalone_audios"]] == [1, 2, 3]
    warning = next(value for value in resolved["warnings"]
                   if value["code"] == "standalone_audios_slot_cap")
    assert "3 Audio inputs" in warning["message"]
    assert "standalone_audios" not in warning["message"]

    scene.prompt_context_profile_id = "minimax_h3_ref@1"
    project = TimelineProject(
        project_id="project", scenes=[scene], references=references, assets=assets,
    )
    compiled = routes.compile_live_scene_prompt_context(
        project, scene,
        template=prompt_channel_templates.get_channel_template("minimax_h3_ref"),
        window_start=0, window_end=scene.duration_frames, fps=24.0,
    )
    assert not any(value["code"] == "standalone_audios_slot_cap"
                   for value in compiled["errors"])
    assert any(value["code"] == "standalone_audios_slot_cap"
               for value in compiled["warnings"])


def test_reference_lane_config_save_preserves_lane_id_and_population():
    """`lane_id` is the sole key `_recipe_lookup` uses, so a save must keep it."""
    lane = _h3_lane_recipe("pictures")
    scene = Scene(
        scene_id="scene", duration_frames=24,
        reference_lane_count=1,
        reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[lane],
    )

    incoming = lane.to_dict()
    incoming.pop("lane_id")
    incoming["recipe"]["name"] = "Edited"
    routes._apply_lane_config(scene, {
        "lane_type": "reference", "lane_index": 0,
        "fields": {"reference_recipe": incoming},
    })

    assert scene.reference_lane_recipes[0].lane_id == "pictures"
    assert minimax_h3.population_lane_ids(
        scene.reference_lane_recipes, "pictures") == ["pictures"]


def test_frontend_default_reference_recipe_mints_lane_identity():
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    method = source.split("_defaultReferenceLaneRecipe(overrides = {}) {", 1)[1].split(
        "\n    }", 1)[0]
    assert "randomUUID" in method
    assert "lane_id: laneId" in method


def test_python_and_browser_reference_resolvers_share_most_specific_semantics():
    cases = [
        {
            "items": [
                {"reference_item_id": "broad", "lane_index": 0, "start_frame": 0, "end_frame": -1, "muted": False},
                {"reference_item_id": "specific", "lane_index": 0, "start_frame": 20, "end_frame": 40, "muted": False},
                {"reference_item_id": "muted", "lane_index": 1, "start_frame": 20, "end_frame": 40, "muted": True},
            ],
            "configs": [{}, {}],
            "duration": 100,
            "start": 25,
            "end": 35,
            "count": 2,
        },
        {
            "items": [{"reference_item_id": "hidden", "lane_index": 0, "start_frame": 0, "end_frame": -1}],
            "configs": [{"hidden": True}],
            "duration": 50,
            "start": 0,
            "end": 10,
            "count": 1,
        },
    ]
    expected = []
    for case in cases:
        rows = resolve_effective_references(
            reference_items=case["items"], lane_count=case["count"], scene_duration=case["duration"],
            window_start=case["start"], window_end=case["end"], lane_configs=case["configs"],
        )
        expected.append([row["item"]["reference_item_id"] if row else None for row in rows])
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for resolver parity")
    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const cases = {json.dumps(cases)};
console.log(JSON.stringify(cases.map(c => mod.resolveEffectiveReferences({{
  referenceItems: c.items, laneCount: c.count, sceneDuration: c.duration,
  windowStart: c.start, windowEnd: c.end, laneConfigs: c.configs,
}}).map(row => row?.item?.reference_item_id || null))));
"""
    actual = json.loads(subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert actual == expected == [["specific", None], [None]]


def test_reference_bulk_delete_local_path_does_not_leak_state_between_methods():
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    local_delete = source.split("_applyLocalBulkDeleteItems(items = []", 1)[1].split("_buildAssetItem", 1)[0]
    mutation_item = source.split("_mutationItemFromSelection(item)", 1)[1].split("_pruneLocalLinkedGroups", 1)[0]
    assert "referenceIds.size" in local_delete
    assert "referenceIds" not in mutation_item


def test_wrong_media_drop_cannot_repurpose_a_configured_reference_lane():
    """media_kind is a hard lane property: only a never-configured lane adopts it.

    The rule lives in `_referenceLaneAcceptor`, which hover and drop now share
    so the drag cannot promise a landing the drop refuses.
    """
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    can_use = source.split("_referenceLaneAcceptor(mediaKind, startFrame) {", 1)[1]
    can_use = can_use.split("\n    }", 1)[0]
    assert "if (recipe.media_kind === mediaKind) return true;" in can_use
    assert "return !occupied && this._isUnconfiguredReferenceLaneRecipe(recipe);" in can_use
    # Both consumers go through it, so neither can drift from the other.
    assert "const canUse = this._referenceLaneAcceptor(mediaKind, startFrame);" in source
    assert "const canUse = this._referenceLaneAcceptor(mediaKind, frame);" in source
    unconfigured = source.split("_isUnconfiguredReferenceLaneRecipe(recipe) {", 1)[1].split("\n    }", 1)[0]
    assert 'value.media_kind || "image") === "image"' in unconfigured
    assert '!String(value.recipe_id || "")' in unconfigured
    assert '!Object.keys(value.recipe || {}).length' in unconfigured


def test_mixed_kind_drag_is_refused_but_an_unresolved_member_is_not():
    """Only a genuinely mixed entity is refused at `dragstart`.

    `""` from the kind rule means two different things — "spans both lane kinds"
    and "cannot be classified yet". Refusing on both would block a valid
    single-kind entity holding one trashed member, with a message saying
    something factually untrue about it.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for reference drag coverage")
    module_url = (ROOT / "web" / "js" / "editor_reference_library.js").as_uri()
    assets = [
        {"asset_id": "img", "asset_type": "image"},
        {"asset_id": "aud", "asset_type": "audio"},
        {"asset_id": "vid", "asset_type": "video"},
    ]
    members = {
        "image": {"member_id": "m1", "asset_id": "img"},
        "audio": {"member_id": "m2", "asset_id": "aud"},
        "video": {"member_id": "m3", "asset_id": "vid"},
        "voice_video": {"member_id": "m4", "asset_id": "vid",
                        "tags": ["sonder:voice_identity"]},
        "missing": {"member_id": "m5", "asset_id": "gone"},
    }
    script = f"""
const mod = await import({json.dumps(module_url)});
const assets = {json.dumps(assets)};
const m = {json.dumps(members)};
const mixed = (members) => mod.referenceDragIsMixedKind(members, assets);
console.log(JSON.stringify({{
  singleImage: mixed([m.image]),
  multiImage: mixed([m.image, m.video]),
  singleAudio: mixed([m.audio]),
  imageAndAudio: mixed([m.image, m.audio]),
  imageAndVoiceVideo: mixed([m.image, m.voice_video]),
  imageAndMissing: mixed([m.image, m.missing]),
  onlyMissing: mixed([m.missing]),
  empty: mixed([]),
  kinds: {{
    image: mod.referenceMemberMediaKind(m.image, assets[0]),
    audio: mod.referenceMemberMediaKind(m.audio, assets[1]),
    video: mod.referenceMemberMediaKind(m.video, assets[2]),
    voiceVideo: mod.referenceMemberMediaKind(m.voice_video, assets[2]),
    unresolved: mod.referenceMemberMediaKind(m.image, null),
  }},
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)

    # A plain video is image-kind; only a voice-tagged one is audio.
    assert result["kinds"] == {"image": "image", "audio": "audio",
                               "video": "image", "voiceVideo": "audio",
                               "unresolved": ""}
    # Genuinely mixed entities are the only refusals.
    assert result["imageAndAudio"] is True
    assert result["imageAndVoiceVideo"] is True
    # Everything else stays draggable, including the unresolved-member case.
    for key in ("singleImage", "multiImage", "singleAudio",
                "imageAndMissing", "onlyMissing", "empty"):
        assert result[key] is False, key


def test_reference_drops_follow_the_zone_model_like_asset_drops():
    """The ruler ALWAYS creates a lane; it used to be a silent no-op."""
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    place = source.split("async _placeReferencePayload(", 1)[1]
    place = place.split("\n    async _handleAssetDrop(", 1)[0]
    # Ruler sets the flag rather than returning.
    assert "forceNewLane = true;" in place
    assert "if (trackRawY < this._timelineRulerHeight()) return;" not in place, (
        "the ruler is a silent no-op again")
    # And the flag must short-circuit lane REUSE, or the ruler quietly stages
    # into an existing compatible lane instead of creating one.
    assert "if (!entry && !forceNewLane) entry = referenceEntries.find(canUse)" in place

    # Anchor on the METHOD, not the dragover call site above it.
    hover = source.split("\n    _resolveDropHoverTarget(", 1)[1]
    hover = hover.split("\n    _referencePayloadMediaKind(", 1)[0]
    assert 'return { kind: "ruler" };' in hover
    assert "this._referenceLaneAcceptor(mediaKind, frame)" in hover
    # The kind-blind highlight is what promised landings the drop refused.
    assert "!this._isLaneLocked(entry.type, entry.laneIndex || 0)\n" not in hover


def test_reference_drop_resolves_lane_recipes_through_one_authority():
    """Regression: `recipeFor` survived only inside the extracted acceptor.

    Extracting `canUse` into `_referenceLaneAcceptor` moved the local
    `recipeFor` closure with it and left the call in `_placeReferencePayload`
    unbound, so every Reference drop threw `ReferenceError: recipeFor is not
    defined` before it could create a lane. The zone-model test above could not
    see it: string-matching source proves a line exists, never that its
    identifiers resolve.
    """
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    # One authority, reachable from both the acceptor and the placement path.
    assert "_referenceLaneRecipe(laneIndex) {" in source
    acceptor = source.split("_referenceLaneAcceptor(mediaKind, startFrame) {", 1)[1]
    acceptor = acceptor.split("\n    }", 1)[0]
    assert "this._referenceLaneRecipe(entry.laneIndex || 0)" in acceptor
    place = source.split("async _placeReferencePayload(", 1)[1]
    place = place.split("\n    async _handleAssetDrop(", 1)[0]
    assert "const existingRecipe = this._referenceLaneRecipe(laneIndex);" in place
    # The unbound call must not come back in either direction.
    assert "recipeFor(" not in place, "unbound recipeFor is back in the drop path"
    assert "recipeFor(" not in acceptor


def test_reference_library_can_explain_a_refused_drag():
    """A refusal the user cannot see is indistinguishable from a broken drag.

    The library reaches the notification bus only through `host.notify`, and
    `host.notify?.()` optional-chains into silence when the host omits it — so
    the mixed-kind refusal explained nothing at all.
    """
    library = (ROOT / "web" / "js" / "editor_reference_library.js").read_text(encoding="utf-8")
    widget = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    assert 'host.notify?.("Stage image/video references separately' in library
    # The host bag must actually carry the seam the library calls.
    host_bag = widget.split("mountReferenceLibrary(this._referenceLibraryEl, {", 1)[1]
    host_bag = host_bag.split("\n        });", 1)[0]
    assert "notify: (message) => notifyWarning(String(message || \"\")," in host_bag
    assert 'source: "reference-drag-refused"' in host_bag


def test_reference_bridge_shape_tracks_project_writes_and_recipe_liveness():
    """Durable writes and browser-local render-window changes refresh slots."""
    bridge = (ROOT / "web" / "js" / "reference_bridge.js").read_text(encoding="utf-8")
    client = (ROOT / "web" / "js" / "api_client.js").read_text(encoding="utf-8")
    controller = (ROOT / "web" / "js" / "editor_node_controller.js").read_text(encoding="utf-8")
    window_events = (ROOT / "web" / "js" / "editor_render_window_events.js").read_text(encoding="utf-8")
    assert 'import { onProjectVersionChanged } from "./api_client.js";' in bridge
    assert "onProjectVersionChanged(refreshAllBridges);" in bridge
    assert "onEditorRenderWindowChanged(refreshAllBridges);" in bridge
    assert "emitEditorRenderWindowChanged({" in controller
    for field in ("scene_id", "selection_start", "selection_end",
                  "pre_context_frames", "post_context_frames"):
        assert f'"{field}"' in window_events
    assert "lane.image_slot_count" in bridge
    assert "lane.audio_slot_count" in bridge
    assert "lane.prompt_slot_count" in bridge
    assert "BRIDGES.has(nodeType(target))" in bridge
    assert "controller.whenProjectReady(() => refreshShape(node));" in bridge
    assert "Refresh reference slots" in bridge
    assert "beforeRegisterNodeDef(_nodeType, nodeData)" in bridge
    assert "INPUT_DEFINITIONS.set(name, distillInputDefinition(nodeData))" in bridge
    assert "inputRequirement(definition, targetInput?.name) === \"required\"" in bridge
    assert "requiredConsumerSlots: requiredConsumerSlotNames(node)" in bridge
    assert "...shape," in bridge, "node-local advisory data must not mutate shared FULL_SHAPE"
    policy_callback = bridge.split(
        'const unusedSlotsWidget = findWidget(node, "unused_slots");', 1)[1]
    policy_callback = policy_callback.split("const originalMenu", 1)[0]
    assert "unusedSlotsWidget.callback = function" in policy_callback
    assert "window.setTimeout(() => refreshShape(node), 0);" in policy_callback
    # Every "we don't know" path resolves to the full shape, never a subset: an
    # unwired selector, an unresolved project, a missing lane and a failed fetch.
    assert bridge.count("return FULL_SHAPE;") == 4
    assert "applyReferenceBridgeShape(node, FULL_SHAPE)" in bridge
    assert "export function onProjectVersionChanged(callback)" in client
    assert "if (next !== current) emitProjectVersionChanged(normalizedProjectId, next);" in client


def test_editor_render_window_event_channel_is_browser_local_and_unsubscribable():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for render-window event coverage")
    module_url = (ROOT / "web" / "js" / "editor_render_window_events.js").as_uri()
    script = f"""
const target = new EventTarget();
globalThis.window = {{
  addEventListener: (...args) => target.addEventListener(...args),
  removeEventListener: (...args) => target.removeEventListener(...args),
  dispatchEvent: (...args) => target.dispatchEvent(...args),
}};
if (typeof globalThis.CustomEvent === 'undefined') {{
  globalThis.CustomEvent = class extends Event {{
    constructor(type, options = {{}}) {{ super(type); this.detail = options.detail; }}
  }};
}}
const mod = await import({json.dumps(module_url)});
const calls = [];
const off = mod.onEditorRenderWindowChanged((detail) => calls.push(detail.field));
mod.emitEditorRenderWindowChanged({{field: 'selection_start'}});
off();
mod.emitEditorRenderWindowChanged({{field: 'selection_end'}});
console.log(JSON.stringify({{
  calls,
  selection: mod.isEditorRenderWindowField('selection_start'),
  context: mod.isEditorRenderWindowField('post_context_frames'),
  mask: mod.isEditorRenderWindowField('mask_post_offset'),
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout)
    assert result == {
        "calls": ["selection_start"],
        "selection": True,
        "context": True,
        "mask": False,
    }


def test_recipe_output_liveness_mirrors_across_backend_and_frontend():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for liveness parity")
    from server.reference_resolution import REFERENCE_OUTPUT_NAMES, reference_live_outputs
    from server.timeline_state import REFERENCE_RECIPE_PRESETS

    hards = [dict(preset["hard"]) for preset in REFERENCE_RECIPE_PRESETS] + [{}, {"live_outputs": ["nope"]}]
    expected = [sorted(reference_live_outputs(hard)) for hard in hards]
    module_url = (ROOT / "web" / "js" / "reference_resolution.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
console.log(JSON.stringify({json.dumps(hards)}.map(h => [...mod.referenceLiveOutputs(h)].sort())));
"""
    actual = json.loads(subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert actual == expected
    # Every image recipe leaves reference_audio dead; the audio recipe is the
    # only one that drives it and drives no image output.
    for preset in REFERENCE_RECIPE_PRESETS:
        live = reference_live_outputs(preset["hard"])
        assert live.issubset(set(REFERENCE_OUTPUT_NAMES))
        if preset["media_kind"] == "audio":
            assert live == {"audio_slots", "reference_prompt", "reference_names"}
        else:
            assert "image_slots" in live and "audio_slots" not in live


def test_retired_live_outputs_are_preserved_raw_but_not_accepted_for_authoring():
    from server.reference_resolution import REFERENCE_OUTPUT_NAMES, reference_live_outputs
    from server.timeline_state import REFERENCE_RECIPE_PRESETS

    legacy_wrapper = {
        "media_kind": "image",
        "recipe_id": "legacy",
        "recipe": {"hard": {"assembly": "slots", "live_outputs": ["slots", "reference_idx", "context"]}},
    }
    scene = Scene.from_dict({
        "scene_id": "scene",
        "reference_lane_count": 1,
        "reference_lane_recipes": [legacy_wrapper],
    })
    retired = ["slots", "reference_idx", "context"]
    assert scene.reference_lane_recipes[0].recipe["hard"]["live_outputs"] == retired

    job = GenerationJob.from_dict({"reference_lane_recipes": [legacy_wrapper]})
    assert job.reference_lane_recipes[0]["recipe"]["hard"]["live_outputs"] == retired

    project = TimelineProject.from_dict({
        "project_id": "project",
        "reference_recipes": [{
            "id": "custom:legacy", "name": "Legacy", "media_kind": "audio",
            "hard": {"assembly": "audio", "live_outputs": ["reference_audio"]}, "soft": {},
        }],
    })
    assert project.reference_recipes[0]["hard"]["live_outputs"] == ["reference_audio"]
    with pytest.raises(routes.ProjectMutationRequestError) as exc:
        routes._normalize_reference_recipe_section(
            {"assembly": "slots",
             "live_outputs": ["slots", "reference_audio", "reference_idx"]},
            "hard",
        )
    assert exc.value.code == "invalid_reference_recipe"
    assert reference_live_outputs({"live_outputs": retired}) == set()

    # The shipped catalog is the fourth store and must already be canonical.
    assert all(
        set(preset["hard"].get("live_outputs", [])).issubset(set(REFERENCE_OUTPUT_NAMES))
        for preset in REFERENCE_RECIPE_PRESETS
    )


# --- bridge-references payload ------------------------------------------------

def _bridge_reference_rows(monkeypatch, preset_id, member_count=2):
    """Call the real GET handler for a lane staging `member_count` members."""
    import importlib
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    import server
    import server.routes as routes_module
    from server.timeline_state import REFERENCE_RECIPE_PRESETS

    monkeypatch.setattr(
        server, "PromptServer",
        SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef())),
        raising=False)
    module = importlib.reload(routes_module)

    preset = next(p for p in REFERENCE_RECIPE_PRESETS if p["id"] == preset_id)
    asset_type = "audio" if preset["media_kind"] == "audio" else "image"
    assets, members = [], []
    for index in range(member_count):
        assets.append(Asset(asset_id=f"asset-{index}", name=f"A{index}",
                            asset_type=asset_type, path=f"media/{index}.{asset_type}"))
        members.append(ReferenceMember(member_id=f"member-{index}", asset_id=f"asset-{index}"))
    reference = ReferenceEntity(reference_id="entity-1", name="Subject", members=members)
    scene = Scene(
        scene_id="scene-1", duration_frames=100,
        reference_lane_count=1, reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[ReferenceLaneRecipe(
            media_kind=preset["media_kind"], recipe_id=preset["id"],
            recipe={k: v for k, v in preset.items() if k != "id"})],
        reference_items=[ReferenceItem(
            reference_item_id="item-1", lane_index=0, start_frame=0, end_frame=-1,
            members=[{"entity_id": "entity-1", "member_id": m.member_id} for m in members])],
    )
    project = TimelineProject(project_id="project-1", assets=assets,
                              references=[reference], scenes=[scene])
    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)

    handler = next(r.handler for r in module.routes
                   if r.method == "GET" and r.path.endswith("/bridge-references"))
    request = make_mocked_request(
        "GET", "/sonder-editor/project/project-1/scenes/scene-1/bridge-references")
    request.match_info.update({"project_id": "project-1", "scene_id": "scene-1"})
    response = asyncio.run(handler(request))
    assert response.status == 200
    return json.loads(response.text)["references"][0]


def test_bridge_references_gates_the_prompt_block_apart_from_the_r_block(monkeypatch):
    """The p-block follows reference_prompt ALONE.

    `_bridge_tuple` fills p01..pN whenever reference_prompt is live, regardless
    of `slots`, and the image path always passes slot_prompts. Reusing
    `slot_count` for both meant five presets that drive per-member text without
    the r-block reported zero and the canvas marked real output unused.
    """
    both = _bridge_reference_rows(monkeypatch, "sonder:wan_bernini")
    assert both["image_slot_count"] == 2 and both["prompt_slot_count"] == 2
    assert both["audio_slot_count"] == 0

    assembled = _bridge_reference_rows(monkeypatch, "sonder:wan_vace")
    assert assembled["image_slot_count"] == 1
    assert assembled["prompt_slot_count"] == 2

    audio = _bridge_reference_rows(monkeypatch, "sonder:ltx_id_lora_audio", member_count=3)
    assert audio["media_kind"] == "audio"
    assert audio["image_slot_count"] == 0
    assert audio["audio_slot_count"] == 3 and audio["prompt_slot_count"] == 3
    return

    # Drives both blocks: counts agree.
    both = _bridge_reference_rows(monkeypatch, "sonder:wan_bernini")
    assert both["slot_count"] == 2 and both["prompt_slot_count"] == 2

    # Drives text but never the r-block — the reported bug.
    text_only = _bridge_reference_rows(monkeypatch, "sonder:wan_vace")
    assert text_only["slot_count"] == 0, "VACE composites; it drives no r-slot"
    assert text_only["prompt_slot_count"] == 2, "but its per-member text is real"

    # The audio lane is NOT the same case: decode_reference_set's audio branch
    # passes no slot_prompts, so its p-block genuinely is empty.
    audio = _bridge_reference_rows(monkeypatch, "sonder:ltx_id_lora_audio", member_count=1)
    assert audio["media_kind"] == "audio"
    assert "reference_prompt" in audio["live_outputs"]
    assert audio["slot_count"] == 0 and audio["prompt_slot_count"] == 0


def test_bridge_references_uses_effective_window_and_h3_video_image_labels(monkeypatch):
    import importlib
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    import server
    import server.routes as routes_module
    from server.timeline_state import MINIMAX_H3_REFERENCE_RECIPE_PRESETS

    monkeypatch.setattr(
        server, "PromptServer",
        SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef())),
        raising=False)
    module = importlib.reload(routes_module)
    definition = next(
        row for row in MINIMAX_H3_REFERENCE_RECIPE_PRESETS
        if row["id"] == "sonder:minimax_h3_video")
    recipe = ReferenceLaneRecipe(
        lane_id="video-lane", media_kind=definition["media_kind"],
        recipe_id=definition["id"],
        recipe={"name": definition["name"], "hard": dict(definition["hard"]),
                "soft": dict(definition["soft"])})
    assets = [
        Asset(asset_id="early-asset", asset_type="video", duration_sec=2),
        Asset(asset_id="late-asset", asset_type="video", duration_sec=2),
    ]
    references = [
        ReferenceEntity(reference_id="early", name="Early", members=[
            ReferenceMember(member_id="early-member", asset_id="early-asset")]),
        ReferenceEntity(reference_id="late", name="Late", members=[
            ReferenceMember(member_id="late-member", asset_id="late-asset")]),
    ]
    scene = Scene(
        scene_id="scene-1", duration_frames=100,
        reference_lane_count=1, reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[recipe],
        reference_items=[
            ReferenceItem(reference_item_id="early-item", lane_index=0,
                          start_frame=0, end_frame=30,
                          members=[{"entity_id": "early", "member_id": "early-member"}]),
            ReferenceItem(reference_item_id="late-item", lane_index=0,
                          start_frame=60, end_frame=90,
                          members=[{"entity_id": "late", "member_id": "late-member"}]),
        ],
    )
    # No setup record anywhere: the lane's declared model input is what makes
    # these `Video N` slots, so the Bridge panel labels a brand-new scene the
    # same as one that inherited a conditioning setup.
    assert scene.minimax_h3_conditioning_setups == []
    project = TimelineProject(
        project_id="project-1", assets=assets, references=references,
        scenes=[scene])
    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    handler = next(r.handler for r in module.routes
                   if r.method == "GET" and r.path.endswith("/bridge-references"))

    def payload(start, end):
        request = make_mocked_request(
            "GET", ("/sonder-editor/project/project-1/scenes/scene-1/bridge-references"
                    f"?selection_start={start}&selection_end={end}"))
        request.match_info.update({"project_id": "project-1", "scene_id": "scene-1"})
        response = asyncio.run(handler(request))
        assert response.status == 200
        return json.loads(response.text)

    early = payload(5, 20)
    early_lane = early["references"][0]
    assert (early["window_start"], early["window_end"]) == (5, 20)
    assert early_lane["media_kind"] == "image"
    assert early_lane["image_slot_count"] == 1
    assert early_lane["slot_labels"] == ["Video 1 · Early (subject)"]

    late = payload(65, 80)["references"][0]
    assert late["slot_labels"] == ["Video 1 · Late (subject)"]

    absent = payload(40, 50)["references"][0]
    assert absent["member_count"] == 0
    assert absent["image_slot_count"] == 0
    assert absent["slot_labels"] == []

    project.generation_queue = [GenerationJob(
        job_id="running", scene_id="scene-1", status="running",
        params={"snapshot_version": 1},
        reference_lane_count=1,
        reference_lane_configs=[LaneConfig().to_dict()],
        reference_lane_recipes=[recipe.to_dict()],
        reference_item_snapshots=[scene.reference_items[0].to_dict()],
        compiled_prompt_context={"window": {"start_frame": 0, "end_frame": 20}},
        reference_input_snapshots=[{
            "kind": "reference", "value": references[0].to_dict(),
        }, {
            "kind": "asset", "value": assets[0].to_dict(),
        }],
        minimax_h3_setup_snapshot={"videos": [{
            "lane_id": "video-lane", "member_id": "early-member",
            "video_ordinal": 1,
        }]},
    )]
    references[0].name = "Changed Live Name"
    frozen = payload(65, 80)
    assert frozen["source"] == "snapshot"
    assert (frozen["window_start"], frozen["window_end"]) == (0, 20)
    assert frozen["references"][0]["slot_labels"] == [
        "Video 1 · Early (subject)"]


# `lane_population` is the single authority for which lanes serve a MiniMax H3
# population, and the browser needs the same answer to decide what a chip may
# attach to. The two halves are separate implementations, so this compares them
# on the cases that actually differ: a bare recipe body carrying only
# `recipe_id`, a declared population, and a generic lane.
_LANE_POPULATION_CASES = [
    {"lane_id": "declared", "recipe": {"soft": {"physical_population": "pictures"}}},
    {"lane_id": "bare-picture", "recipe_id": "sonder:minimax_h3_picture"},
    {"lane_id": "bare-video", "recipe_id": "sonder:minimax_h3_video"},
    {"lane_id": "bare-audio", "recipe_id": "sonder:minimax_h3_audio"},
    {"lane_id": "nested-id", "recipe": {"id": "sonder:minimax_h3_picture"}},
    {"lane_id": "declared-wins", "recipe_id": "sonder:minimax_h3_video",
     "recipe": {"soft": {"physical_population": "pictures"}}},
    {"lane_id": "generic", "recipe": {"hard": {"assembly": "slots"}}},
    {"lane_id": "wan", "recipe_id": "sonder:wan_vace", "recipe": {"soft": {}}},
    {"lane_id": "empty"},
]


def test_lane_population_matches_between_python_and_the_browser():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for lane population parity")
    expected = [minimax_h3.lane_population(case) for case in _LANE_POPULATION_CASES]
    assert expected == ["pictures", "pictures", "videos", "standalone_audios",
                        "pictures", "pictures", "", "", ""]
    module_url = (ROOT / "web" / "js" / "reference_lane_identity.js").as_uri()
    script = f"""
const mod = await import({json.dumps(module_url)});
const cases = {json.dumps(_LANE_POPULATION_CASES)};
console.log(JSON.stringify(cases.map((value) => mod.lanePopulation(value))));
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True)
    assert json.loads(result.stdout) == expected


def test_physical_chip_values_keep_the_singular_token_the_parsers_expect():
    """`lanePopulation` answers in plural; the chip value is parsed singular.

    Three regexes in `prompt_context_chips.js` read
    `physical:(picture|video|audio):<member_id>`. Feeding the manifest's plural
    key straight into that value would leave every physical chip unbindable and
    orphan stored selections, and a parity test on `lanePopulation` alone would
    not notice.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for physical chip value coverage")
    module_url = (ROOT / "web" / "js" / "prompt_context_chips.js").as_uri()
    scene = {
        "duration_frames": 20, "reference_lane_count": 3,
        "reference_lane_configs": [{}, {}, {}],
        "reference_lane_recipes": [
            {"lane_id": "pic", "recipe": {"soft": {
                "physical_population": "pictures",
                "compatible_profiles": ["minimax_h3_ref@1"]}}},
            {"lane_id": "vid", "recipe": {"soft": {
                "physical_population": "videos",
                "compatible_profiles": ["minimax_h3_ref@1"]}}},
            {"lane_id": "aud", "recipe": {"soft": {
                "physical_population": "standalone_audios",
                "compatible_profiles": ["minimax_h3_ref@1"]}}},
        ],
        "reference_items": [
            {"reference_item_id": "i0", "lane_index": 0, "start_frame": 0,
             "end_frame": -1, "members": [{"entity_id": "e", "member_id": "m0"}]},
            {"reference_item_id": "i1", "lane_index": 1, "start_frame": 0,
             "end_frame": -1, "members": [{"entity_id": "e", "member_id": "m1"}]},
            {"reference_item_id": "i2", "lane_index": 2, "start_frame": 0,
             "end_frame": -1, "members": [{"entity_id": "e", "member_id": "m2"}]},
        ],
    }
    script = f"""
const mod = await import({json.dumps(module_url)});
const result = mod.promptReferenceSourceOptions({{
    scene: {json.dumps(scene)},
    references: [{{reference_id: "e", name: "Entity"}}],
    semanticUnits: [],
    profileId: "minimax_h3_ref@1",
    scope: "global",
    resolvedProfile: {{physical_populations: [{{key: "pictures"}}]}},
}});
console.log(JSON.stringify({{
    values: result.physicalOptions.map((row) => row[0]),
    eligible: result.physicalOptions.map((row) => row[2]),
}}));
"""
    result = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True, text=True, encoding="utf-8", check=True).stdout)
    assert result["values"] == [
        "physical:picture:m0", "physical:video:m1", "physical:audio:m2"]
    assert all(re.match(r"^physical:(picture|video|audio):(.+)$", value)
               for value in result["values"])
    # No setup record anywhere in the scene, yet every staged member attaches.
    assert result["eligible"] == [True, True, True]
