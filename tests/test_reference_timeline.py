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
    assert "minimax_h3_conditioning_setups" not in scene.to_dict()

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


def _drop_verdicts(cases):
    """Run the shared Reference drop resolver under node and return verdicts.

    Behavioral rather than source-string: the tests this replaced asserted exact
    lines inside `_referenceLaneAcceptor`, and rewriting a string test to match
    the strings you just wrote proves nothing. The rule itself is pure and lives
    in `reference_lane_identity.js` precisely so it can be executed.
    """
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Reference drop resolver coverage")
    module_url = (ROOT / "web" / "js" / "reference_lane_identity.js").as_uri()
    script = """
const mod = await import(%s);
const cases = %s;
const out = {};
for (const [name, spec] of Object.entries(cases)) {
  out[name] = mod.resolveReferenceDropVerdict(spec.lane, spec.options);
}
console.log(JSON.stringify(out));
""" % (json.dumps(module_url), json.dumps(cases))
    return json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)


def _lane(recipe, items=(), **flags):
    return {"laneIndex": 0, "collapsed": False, "locked": False,
            "recipe": recipe, "items": list(items), **flags}


def _options(media_kind="image", frame=10, members=(("m1", "image"),),
             allow_append=False):
    return {
        "mediaKind": media_kind,
        "frame": frame,
        "sceneDuration": 100,
        "members": [{"member_id": mid, "assetType": kind} for mid, kind in members],
        "allowAppend": allow_append,
    }


_IMAGE_LANE = {"media_kind": "image", "recipe_id": "", "recipe": {}}
_AUDIO_LANE = {"media_kind": "audio", "recipe_id": "sonder:generic_audio",
               "recipe": {"soft": {}}}
_H3_VIDEO_LANE = {"media_kind": "image", "recipe_id": "sonder:minimax_h3_video",
                  "recipe": {"soft": {"physical_population": "videos"}}}
_H3_PICTURE_LANE = {"media_kind": "image", "recipe_id": "sonder:minimax_h3_picture",
                    "recipe": {"soft": {"physical_population": "pictures"}}}
_BARE_RECIPE_ID_LANE = {"media_kind": "image",
                        "recipe_id": "sonder:minimax_h3_picture", "recipe": {}}
_STAGED = [{"reference_item_id": "staged", "start_frame": 0, "end_frame": -1,
            "members": [{"member_id": "already"}]}]


def test_wrong_media_drop_cannot_repurpose_a_configured_reference_lane():
    """media_kind is a hard lane property: only a never-configured lane adopts it."""
    verdicts = _drop_verdicts({
        # A blank lane adopts the dragged kind.
        "adopt": {"lane": _lane(_IMAGE_LANE),
                  "options": _options("audio", members=(("m1", "audio"),))},
        # A configured lane of the other kind does not.
        "configured": {"lane": _lane(_AUDIO_LANE), "options": _options("image")},
        # Neither does a blank-recipe lane that already holds an item.
        "occupiedBlank": {"lane": _lane(_IMAGE_LANE, _STAGED),
                          "options": _options("audio", frame=90,
                                              members=(("m1", "audio"),))},
        "locked": {"lane": _lane(_IMAGE_LANE, locked=True), "options": _options()},
        "collapsed": {"lane": _lane(_IMAGE_LANE, collapsed=True), "options": _options()},
    })
    assert verdicts["adopt"]["verdict"] == "create"
    assert verdicts["configured"] == {"verdict": "reject", "reason": "media_kind",
                                      "itemId": ""}
    assert verdicts["occupiedBlank"]["reason"] == "media_kind"
    assert verdicts["locked"]["reason"] == "locked"
    assert verdicts["collapsed"]["reason"] == "collapsed"


def test_reference_hover_narrows_by_population_exactly_as_staging_does():
    """An H3 Video lane carries `media_kind: "image"` but takes only video.

    The acceptor compared only `media_kind`, so a still image highlighted as a
    valid landing and the drop was then refused with
    `reference_media_kind_mismatch` - the honesty gap the shared predicate
    exists to prevent.
    """
    verdicts = _drop_verdicts({
        "stillOnVideoLane": {"lane": _lane(_H3_VIDEO_LANE), "options": _options()},
        "videoOnVideoLane": {"lane": _lane(_H3_VIDEO_LANE),
                             "options": _options(members=(("m1", "video"),))},
        "videoOnPictureLane": {"lane": _lane(_H3_PICTURE_LANE),
                               "options": _options(members=(("m1", "video"),))},
        "stillOnPictureLane": {"lane": _lane(_H3_PICTURE_LANE), "options": _options()},
        # The divergence case: a bare-bodied lane carrying only `recipe_id`
        # resolves to "" server-side (image OR video legal). Mirroring
        # `lanePopulation`'s `recipe_id` fallback here would make the client
        # STRICTER than the server and silently block a legal staging.
        "bareRecipeIdTakesVideo": {"lane": _lane(_BARE_RECIPE_ID_LANE),
                                   "options": _options(members=(("m1", "video"),))},
        "bareRecipeIdTakesImage": {"lane": _lane(_BARE_RECIPE_ID_LANE),
                                   "options": _options()},
    })
    assert verdicts["stillOnVideoLane"]["reason"] == "population"
    assert verdicts["videoOnVideoLane"]["verdict"] == "create"
    assert verdicts["videoOnPictureLane"]["reason"] == "population"
    assert verdicts["stillOnPictureLane"]["verdict"] == "create"
    assert verdicts["bareRecipeIdTakesVideo"]["verdict"] == "create"
    assert verdicts["bareRecipeIdTakesImage"]["verdict"] == "create"


def test_reference_append_needs_an_explicit_pointer_hit_on_the_bar():
    """`addToTimeline` passes no coordinates; it must never silently append."""
    verdicts = _drop_verdicts({
        "noCoordinates": {"lane": _lane(_IMAGE_LANE, _STAGED),
                          "options": _options(allow_append=False)},
        "onTheBar": {"lane": _lane(_IMAGE_LANE, _STAGED),
                     "options": _options(allow_append=True)},
        # A frame the bar does not cover still creates, even with append allowed.
        "pastTheBar": {"lane": _lane(_IMAGE_LANE, [
            {"reference_item_id": "staged", "start_frame": 0, "end_frame": 20,
             "members": [{"member_id": "already"}]}]),
            "options": _options(frame=50, allow_append=True)},
        "duplicateMember": {"lane": _lane(_IMAGE_LANE, _STAGED),
                            "options": _options(members=(("already", "image"),),
                                                allow_append=True)},
        # Population narrowing runs BEFORE occupancy, so an append cannot slip a
        # member past a rule a create would have refused.
        "appendNarrows": {"lane": _lane(_H3_VIDEO_LANE, _STAGED),
                          "options": _options(allow_append=True)},
    })
    assert verdicts["noCoordinates"] == {"verdict": "reject", "reason": "occupied",
                                         "itemId": ""}
    assert verdicts["onTheBar"] == {"verdict": "append", "reason": "",
                                    "itemId": "staged"}
    assert verdicts["pastTheBar"]["verdict"] == "create"
    assert verdicts["duplicateMember"]["reason"] == "duplicate_member"
    assert verdicts["appendNarrows"]["reason"] == "population"


def test_reference_hover_and_drop_run_the_same_resolver():
    """One resolver, two call sites - the highlight cannot outrun the drop."""
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    hover = source.split("\n    _resolveDropHoverTarget(", 1)[1]
    hover = hover.split("\n    /** The effective recipe of one Reference lane", 1)[0]
    assert "this._referenceDropResolver(referenceDrag, frame)" in hover
    place = source.split("async _placeReferencePayloadWithinGesture(", 1)[1]
    place = place.split("\n    async _handleAssetDrop(", 1)[0]
    assert "this._referenceDropResolver(payload, startFrame)" in place
    # The create-only predicate is what `find()` uses, so the no-coordinate
    # `addToTimeline` path can never reach the append verdict.
    assert 'const canUse = (entry) => resolve(entry).verdict === "create";' in place
    assert "if (!entry && !forceNewLane) entry = referenceEntries.find(canUse)" in place


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
    assert "this._referenceDropResolver(referenceDrag, frame)" in hover
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
    facts = source.split("_referenceLaneFacts(entry) {", 1)[1]
    facts = facts.split("\n    }", 1)[0]
    assert "this._referenceLaneRecipe(laneIndex)" in facts
    place = source.split("async _placeReferencePayload(", 1)[1]
    place = place.split("\n    async _handleAssetDrop(", 1)[0]
    assert "const existingRecipe = this._referenceLaneRecipe(laneIndex);" in place
    # The unbound call must not come back in either direction.
    assert "recipeFor(" not in place, "unbound recipeFor is back in the drop path"
    assert "recipeFor(" not in facts


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
    shape = (ROOT / "web" / "js" / "reference_bridge_shape.js").read_text(encoding="utf-8")
    client = (ROOT / "web" / "js" / "api_client.js").read_text(encoding="utf-8")
    controller = (ROOT / "web" / "js" / "editor_node_controller.js").read_text(encoding="utf-8")
    window_events = (ROOT / "web" / "js" / "editor_render_window_events.js").read_text(encoding="utf-8")
    assert 'import { onProjectVersionChanged } from "./api_client.js";' in bridge
    assert "onProjectVersionChanged(refreshAllBridges);" in bridge
    assert "onEditorRenderWindowChanged(refreshAllBridgesForWindow);" in bridge
    assert "emitEditorRenderWindowChanged({" in controller
    for field in ("scene_id", "selection_start", "selection_end",
                  "pre_context_frames", "post_context_frames"):
        assert f'"{field}"' in window_events
    assert "mergedBridgeShape({" in bridge
    assert "lane?.image_slot_count" in shape
    assert "lane?.audio_slot_count" in shape
    assert "lane?.prompt_slot_count" in shape
    assert "BRIDGES.has(nodeType(target))" in bridge
    assert 'origin: "project_ready"' in bridge
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
    assert 'origin: "bridge_widget"' in policy_callback
    # Every transport/source "we don't know" path resolves to the full shape,
    # never a subset: an unwired selector, an unresolved project, or failed
    # project readiness. Orphan-only selections reach the same fail-open result
    # through mergedBridgeShape's null liveness contract.
    assert bridge.count("return FULL_SHAPE;") == 3
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


def test_bridge_reference_route_entry_diagnostic_records_fast_physical_dispatch(monkeypatch):
    import importlib
    from types import SimpleNamespace

    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    import server
    import server.routes as routes_module

    monkeypatch.setattr(
        server, "PromptServer",
        SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef())),
        raising=False,
    )
    module = importlib.reload(routes_module)
    project = TimelineProject(
        project_id="project-1",
        scenes=[Scene(scene_id="scene-1", duration_frames=100)],
    )
    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    events = []
    monkeypatch.setattr(
        module,
        "record_diag_event",
        lambda kind, **details: events.append((kind, details)),
    )

    handler = next(
        route.handler for route in module.routes
        if route.method == "GET" and route.path.endswith("/bridge-references")
    )
    request = make_mocked_request(
        "GET",
        "/sonder-editor/project/project-1/scenes/scene-1/bridge-references"
        "?selection_start=5&selection_end=20",
        headers={
            "X-Sonder-Reference-Request-Id": "reference-test-1",
            "X-Sonder-Reference-Generation": "project:project-1:v2",
            "X-Sonder-Reference-Origin": "project_version",
        },
    )
    request.match_info.update({"project_id": "project-1", "scene_id": "scene-1"})

    response = asyncio.run(handler(request))

    assert response.status == 200
    assert events == [(
        "bridge_references_route_entry",
        {
            "project_id": "project-1",
            "rel_url": (
                "/sonder-editor/project/project-1/scenes/scene-1/bridge-references"
                "?selection_start=5&selection_end=20"
            ),
            "request_id": "reference-test-1",
            "generation": "project:project-1:v2",
            "origin": "project_version",
        },
    )]


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
    assert both["reserved_member_span"] == 2
    assert both["image_slot_labels"] == both["slot_labels"]

    assembled = _bridge_reference_rows(monkeypatch, "sonder:wan_vace")
    assert assembled["image_slot_count"] == 1
    assert assembled["prompt_slot_count"] == 2
    assert assembled["image_slot_labels"] == [
        "Assembled · Subject (subject) + Subject (subject)"]

    audio = _bridge_reference_rows(monkeypatch, "sonder:ltx_id_lora_audio", member_count=3)
    assert audio["media_kind"] == "audio"
    assert audio["image_slot_count"] == 0
    assert audio["audio_slot_count"] == 3 and audio["prompt_slot_count"] == 3
    return


def test_bridge_references_reserves_the_widest_item_and_labels_assembled_payloads_per_lane(monkeypatch):
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
    preset = next(p for p in REFERENCE_RECIPE_PRESETS if p["id"] == "sonder:wan_vace")
    assets, references = [], []
    for index in range(4):
        assets.append(Asset(
            asset_id=f"asset-{index}", name=f"Asset {index}", asset_type="image",
            path=f"media/{index}.png"))
        references.append(ReferenceEntity(
            reference_id=f"entity-{index}", name=f"Lane {index // 2} member {index % 2}",
            members=[ReferenceMember(member_id=f"member-{index}", asset_id=f"asset-{index}")]))

    def recipe(index):
        return ReferenceLaneRecipe(
            lane_id=f"lane-{index}", media_kind=preset["media_kind"], recipe_id=preset["id"],
            recipe={key: value for key, value in preset.items() if key != "id"})

    scene = Scene(
        scene_id="scene-1", duration_frames=100, reference_lane_count=2,
        reference_lane_configs=[LaneConfig(), LaneConfig()],
        reference_lane_recipes=[recipe(0), recipe(1)],
        reference_items=[
            ReferenceItem(
                reference_item_id="lane-0-now", lane_index=0, start_frame=0, end_frame=40,
                members=[{"entity_id": "entity-0", "member_id": "member-0"}]),
            ReferenceItem(
                reference_item_id="lane-0-later", lane_index=0, start_frame=40, end_frame=-1,
                members=[
                    {"entity_id": "entity-0", "member_id": "member-0"},
                    {"entity_id": "entity-1", "member_id": "member-1"},
                ]),
            ReferenceItem(
                reference_item_id="lane-1-now", lane_index=1, start_frame=0, end_frame=-1,
                members=[
                    {"entity_id": "entity-2", "member_id": "member-2"},
                    {"entity_id": "entity-3", "member_id": "member-3"},
                ]),
        ],
    )
    project = TimelineProject(
        project_id="project-1", assets=assets, references=references, scenes=[scene])
    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    handler = next(r.handler for r in module.routes
                   if r.method == "GET" and r.path.endswith("/bridge-references"))
    request = make_mocked_request(
        "GET", "/sonder-editor/project/project-1/scenes/scene-1/bridge-references"
               "?selection_start=5&selection_end=20")
    request.match_info.update({"project_id": "project-1", "scene_id": "scene-1"})
    response = asyncio.run(handler(request))
    assert response.status == 200
    lane_0, lane_1 = json.loads(response.text)["references"]

    assert lane_0["member_count"] == 1
    assert lane_0["reserved_member_span"] == 2
    assert lane_0["slot_labels"] == ["Lane 0 member 0 (subject)", "(unused)"]
    assert lane_0["image_slot_count"] == 1
    assert lane_0["image_slot_labels"] == ["Assembled · Lane 0 member 0 (subject)"]
    assert lane_1["image_slot_labels"] == [
        "Assembled · Lane 1 member 0 (subject) + Lane 1 member 1 (subject)"]

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
    assert "minimax_h3_conditioning_setups" not in scene.to_dict()
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
    assert len(early["tag_presets"]) == 26
    assert early["tag_families"] == {
        "minimax_h3": {"label": "MiniMax H3", "short": "H3"}}
    assert (early["window_start"], early["window_end"]) == (5, 20)
    assert early_lane["media_kind"] == "image"
    assert early_lane["image_slot_count"] == 1
    assert early_lane["slot_labels"] == ["Video 1 · Early (subject)"]

    late = payload(65, 80)["references"][0]
    assert late["slot_labels"] == ["Video 1 · Late (subject)"]

    absent = payload(40, 50)["references"][0]
    assert absent["member_count"] == 0
    assert absent["reserved_member_span"] == 1
    assert absent["image_slot_count"] == 1
    assert absent["slot_labels"] == ["(unused)"]
    assert absent["image_slot_labels"] == ["(unused)"]

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
    assert frozen["tag_presets"] == []
    assert frozen["tag_families"] == {}


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
# ── Lane move ─────────────────────────────────────────────────────────────


def _movable_scene():
    _, scene = _project_with_reference("image")
    scene.reference_lane_count = 3
    scene.reference_lane_configs = [LaneConfig(name="A"), LaneConfig(name="B"),
                                    LaneConfig(name="C")]
    scene.reference_lane_recipes = [
        ReferenceLaneRecipe(lane_id="lane-a", recipe_id="a"),
        ReferenceLaneRecipe(lane_id="lane-b", recipe_id="b"),
        ReferenceLaneRecipe(lane_id="lane-c", recipe_id="c"),
    ]
    return scene


def _expected_move(scene, first, second):
    return {"from_lane_id": scene.reference_lane_recipes[first].lane_id,
            "to_lane_id": scene.reference_lane_recipes[second].lane_id}


def test_move_lane_swaps_items_configs_and_recipes_together():
    """Every array keyed by lane index moves at once, `lane_id` included."""
    scene = _movable_scene()
    scene.reference_items = [
        ReferenceItem(reference_item_id="on-a", lane_index=0, start_frame=0, end_frame=10,
                      members=[{"entity_id": "entity-1", "member_id": "member-1"}]),
        ReferenceItem(reference_item_id="on-b", lane_index=1, start_frame=0, end_frame=10,
                      members=[{"entity_id": "entity-1", "member_id": "member-1"}]),
        ReferenceItem(reference_item_id="on-c", lane_index=2, start_frame=0, end_frame=10,
                      members=[{"entity_id": "entity-1", "member_id": "member-1"}]),
    ]

    routes._move_media_lane(scene, "reference", 0, 1, _expected_move(scene, 0, 1))

    assert [recipe.recipe_id for recipe in scene.reference_lane_recipes] == ["b", "a", "c"]
    # The recipe object moves WHOLE: `lane_id` travels with it and is never
    # regenerated, because it is the only key the H3 slot resolver looks a lane
    # up by.
    assert [recipe.lane_id for recipe in scene.reference_lane_recipes] == [
        "lane-b", "lane-a", "lane-c"]
    assert [config.name for config in scene.reference_lane_configs] == ["B", "A", "C"]
    assert {item.reference_item_id: item.lane_index
            for item in scene.reference_items} == {"on-a": 1, "on-b": 0, "on-c": 2}
    assert scene.reference_lane_count == 3


def test_move_lane_pads_short_config_and_recipe_arrays_before_swapping():
    """Either array may legitimately be shorter than the lane count."""
    scene = _movable_scene()
    scene.reference_lane_configs = [LaneConfig(name="A")]
    scene.reference_lane_recipes = [ReferenceLaneRecipe(lane_id="lane-a", recipe_id="a")]

    padded_expected = {"from_lane_id": "lane-a", "to_lane_id": ""}
    with pytest.raises(routes.ProjectMutationRequestError) as mismatch:
        routes._move_media_lane(scene, "reference", 0, 1, padded_expected)
    # Padding minted a real durable id rather than leaving a blank, so the guard
    # refuses instead of moving a lane whose identity the caller never saw.
    assert mismatch.value.code == "identity_mismatch"
    assert len(scene.reference_lane_recipes) == 3
    assert len(scene.reference_lane_configs) == 3

    routes._move_media_lane(scene, "reference", 0, 1, _expected_move(scene, 0, 1))
    assert scene.reference_lane_recipes[1].recipe_id == "a"


def test_move_lane_refusals_cover_identity_locks_range_and_lane_type():
    scene = _movable_scene()
    with pytest.raises(routes.ProjectMutationRequestError) as stale:
        routes._move_media_lane(scene, "reference", 0, 1,
                                {"from_lane_id": "lane-a", "to_lane_id": "lane-c"})
    assert stale.value.code == "identity_mismatch"

    with pytest.raises(routes.ProjectMutationRequestError) as missing:
        routes._move_media_lane(scene, "reference", 0, 1, None)
    assert missing.value.code == "identity_mismatch"

    with pytest.raises(routes.ProjectMutationRequestError) as out_of_range:
        routes._move_media_lane(scene, "reference", 0, 9, _expected_move(scene, 0, 0))
    assert out_of_range.value.code == "item_not_found"

    with pytest.raises(routes.ProjectMutationRequestError) as same:
        routes._move_media_lane(scene, "reference", 1, 1, _expected_move(scene, 1, 1))
    assert same.value.code == "invalid_lane_operation"

    scene.reference_lane_configs[1].locked = True
    with pytest.raises(routes.ProjectMutationRequestError) as locked:
        routes._move_media_lane(scene, "reference", 0, 1, _expected_move(scene, 0, 1))
    assert locked.value.code == "track_locked"
    scene.reference_lane_configs[1].locked = False

    # A generic op accepting any lane_type because the client offers only one
    # would be an untested video/audio reorder waiting to be called, and video
    # lane order is compositing order.
    for lane_type in ("video", "audio", "motion_driver"):
        with pytest.raises(routes.ProjectMutationRequestError) as refused:
            routes._move_media_lane(scene, lane_type, 0, 1, {})
        assert "Cannot move lane type" in str(refused.value)
    # Nothing above mutated the scene.
    assert [recipe.recipe_id for recipe in scene.reference_lane_recipes] == ["a", "b", "c"]


def test_h3_ordinals_and_over_cap_truncation_follow_a_lane_move():
    """The capability, and the invisible consequence that rides with it."""
    first_entity, first_asset = _h3_picture("woman", "portrait")
    second_entity, second_asset = _h3_picture("man", "headshot")
    recipes = [_h3_lane_recipe("lane-a"), _h3_lane_recipe("lane-b")]
    items = [_picture_item("a", 0, "woman", "portrait"),
             _picture_item("b", 1, "man", "headshot")]
    scene = _h3_scene(recipes, items)

    before = _h3_resolve(scene, [first_entity, second_entity], [first_asset, second_asset])
    assert [(row["member_id"], row["picture_ordinal"])
            for row in before["setup_manifest"]["pictures"]] == [
        ("portrait", 1), ("headshot", 2)]

    routes._move_media_lane(scene, "reference", 0, 1,
                            {"from_lane_id": "lane-a", "to_lane_id": "lane-b"})

    after = _h3_resolve(scene, [first_entity, second_entity], [first_asset, second_asset])
    assert [(row["member_id"], row["picture_ordinal"])
            for row in after["setup_manifest"]["pictures"]] == [
        ("headshot", 1), ("portrait", 2)]


def test_move_lane_changes_which_member_an_over_cap_population_drops():
    """`collect()` accumulates in lane order and then truncates `rows[:cap]`."""
    entities = []
    assets = []
    lane_items = []
    # 10 Pictures over two lanes; H3 exposes 9, so exactly one loses its ordinal.
    for index in range(10):
        entity, asset = _h3_picture(f"e{index}", f"m{index}")
        entities.append(entity)
        assets.append(asset)
    first_members = [{"entity_id": f"e{i}", "member_id": f"m{i}"} for i in range(5)]
    second_members = [{"entity_id": f"e{i}", "member_id": f"m{i}"} for i in range(5, 10)]
    lane_items.append(ReferenceItem(reference_item_id="first", lane_index=0,
                                    start_frame=0, end_frame=-1, members=first_members))
    lane_items.append(ReferenceItem(reference_item_id="second", lane_index=1,
                                    start_frame=0, end_frame=-1, members=second_members))
    scene = _h3_scene([_h3_lane_recipe("lane-a"), _h3_lane_recipe("lane-b")], lane_items)

    before = {row["member_id"] for row in
              _h3_resolve(scene, entities, assets)["setup_manifest"]["pictures"]}
    routes._move_media_lane(scene, "reference", 0, 1,
                            {"from_lane_id": "lane-a", "to_lane_id": "lane-b"})
    after = {row["member_id"] for row in
             _h3_resolve(scene, entities, assets)["setup_manifest"]["pictures"]}

    assert len(before) == len(after) == 9
    assert before != after, "the dropped member must change with lane order"


# ── Item split ────────────────────────────────────────────────────────────


def _split_scene(end_frame=-1, members=None):
    project, scene = _project_with_reference("image")
    scene.reference_lane_recipes[0] = ReferenceLaneRecipe(
        lane_id="lane-a", media_kind="image",
        recipe={"soft": {"role_fields": ["visual_intent", "audio_intent", "role"]}})
    scene.reference_items = [ReferenceItem(
        reference_item_id="item", lane_index=0, start_frame=10, end_frame=end_frame,
        members=members or [{"entity_id": "entity-1", "member_id": "member-1"}],
        prompt_override="portrait", strength=0.4, sequence_frames=17, muted=True,
    )]
    return project, scene


def _split_expected(scene, item_id="item"):
    item = next(value for value in scene.reference_items
                if value.reference_item_id == item_id)
    return {
        "reference_item_id": item.reference_item_id,
        "start_frame": item.start_frame,
        "end_frame": item.end_frame,
        "resolved_end_frame": routes._reference_item_resolved_end(scene, item),
    }


def test_split_reference_item_copies_staging_verbatim_and_keeps_the_sentinel():
    """A create round-trip cannot be trusted with staged member fields.

    `_apply_create_reference_item` does not pass `legacy_members` while
    `_apply_update_reference_item` does, so a stored-and-valid `role` that an
    update tolerates through the `unchanged()` escape is refused on create.
    A dedicated op deep-copies the member dicts instead.
    """
    staged = [{"entity_id": "entity-1", "member_id": "member-1",
               "visual_intent": "preserve", "role": "retired_alias"}]
    project, scene = _split_scene(members=staged)

    result = routes._apply_split_reference_item(scene, {
        "reference_item_id": "item", "frame": 50,
        "expected": _split_expected(scene),
    })

    left = next(value for value in scene.reference_items
                if value.reference_item_id == "item")
    right = next(value for value in scene.reference_items
                 if value.reference_item_id == result["right_reference_item_id"])
    assert (left.start_frame, left.end_frame) == (10, 50)
    # `-1` stays on the RIGHT half: a block running to scene end keeps following
    # scene end on the half that should.
    assert (right.start_frame, right.end_frame) == (50, -1)
    assert right.members == staged and left.members == staged
    assert right.members is not left.members
    assert (right.prompt_override, right.strength, right.sequence_frames, right.muted) == (
        "portrait", 0.4, 17, True)
    assert right.lane_index == 0

    # The contrast the op exists for, on the SAME lane and the SAME recipe, so
    # the only difference is that a create passes no `legacy_members`: an update
    # tolerates the stored role through the `unchanged()` escape, a create
    # refuses it, and a client-side shrink-plus-create would have destroyed it.
    tolerated = routes._apply_update_reference_item(project, scene, {
        "reference_item_id": "item",
        "fields": {"members": staged},
        "expected": {"members": staged},
    })
    assert tolerated.members[0]["role"] == "retired_alias"
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_create_reference_item(project, scene, {
            "lane_index": 0, "start_frame": 0, "end_frame": 5, "members": staged})
    assert refused.value.code == "unsupported_reference_role"


def test_split_reference_item_keeps_an_authored_end_on_the_right_half():
    _, scene = _split_scene(end_frame=80)
    result = routes._apply_split_reference_item(scene, {
        "reference_item_id": "item", "frame": 40,
        "expected": _split_expected(scene),
    })
    right = next(value for value in scene.reference_items
                 if value.reference_item_id == result["right_reference_item_id"])
    assert (right.start_frame, right.end_frame) == (40, 80)


def test_split_reference_item_refuses_out_of_range_stale_end_and_locks():
    _, scene = _split_scene()
    for frame in (10, 9, 100, 200):
        with pytest.raises(routes.ProjectMutationRequestError) as invalid:
            routes._apply_split_reference_item(scene, {
                "reference_item_id": "item", "frame": frame,
                "expected": _split_expected(scene)})
        assert invalid.value.code == "invalid_range", frame

    # A concurrent duration change moves the real bound under an `end_frame`
    # of -1 without touching any stored field, which is why the resolved end is
    # in the guard at all.
    stale = _split_expected(scene)
    scene.duration_frames = 200
    with pytest.raises(routes.ProjectMutationRequestError) as mismatch:
        routes._apply_split_reference_item(scene, {
            "reference_item_id": "item", "frame": 50, "expected": stale})
    assert mismatch.value.code == "identity_mismatch"

    with pytest.raises(routes.ProjectMutationRequestError) as missing:
        routes._apply_split_reference_item(scene, {
            "reference_item_id": "item", "frame": 50,
            "expected": {"reference_item_id": "item"}})
    assert missing.value.code == "missing_expected_identity"

    scene.reference_lane_configs[0].locked = True
    with pytest.raises(routes.ProjectMutationRequestError) as locked:
        routes._apply_split_reference_item(scene, {
            "reference_item_id": "item", "frame": 50,
            "expected": _split_expected(scene)})
    assert locked.value.code == "track_locked"
    assert len(scene.reference_items) == 1


def test_split_reference_item_does_not_move_any_bridge_payload():
    """`reserved_span` is a max over the lane; both halves carry one member list."""
    _, scene = _split_scene()
    before = max(len(item.members) for item in scene.reference_items)
    routes._apply_split_reference_item(scene, {
        "reference_item_id": "item", "frame": 50, "expected": _split_expected(scene)})
    after = max(len(item.members) for item in scene.reference_items)
    assert before == after == 1


def test_split_reference_item_reports_a_bound_chip_rather_than_cloning_it():
    """Cloning would be unresolvable exactly where the original resolves."""
    from server.timeline_state import PromptSection

    _, scene = _split_scene()
    bound = {"attachment_id": "chip-1", "kind": "reference", "enabled": True,
             "source": {"reference_item_id": "item"}}
    disabled = {"attachment_id": "chip-2", "kind": "reference", "enabled": False,
                "source": {"reference_item_id": "item"}}
    other = {"attachment_id": "chip-3", "kind": "reference", "enabled": True,
             "source": {"reference_item_id": "somewhere-else"}}
    scene.global_attachments = [dict(bound)]
    scene.prompt_sections = [PromptSection(
        start_frame=0, end_frame=100,
        attachments=[dict(bound), dict(disabled), dict(other)])]

    result = routes._apply_split_reference_item(scene, {
        "reference_item_id": "item", "frame": 50, "expected": _split_expected(scene)})

    assert result["bound_attachment_count"] == 2
    right_id = result["right_reference_item_id"]
    bindings = [str((value.get("source") or {}).get("reference_item_id") or "")
                for value in scene.global_attachments
                + scene.prompt_sections[0].attachments]
    assert right_id not in bindings


# ── Member append ─────────────────────────────────────────────────────────


def test_append_refuses_a_member_already_staged_on_the_item():
    project, scene = _project_with_reference("image")
    scene.reference_items = [ReferenceItem(
        reference_item_id="item", lane_index=0, start_frame=0, end_frame=-1,
        members=[{"entity_id": "entity-1", "member_id": "member-1"}])]
    with pytest.raises(routes.ProjectMutationRequestError) as duplicate:
        routes._apply_update_reference_item(project, scene, {
            "reference_item_id": "item",
            "fields": {"members": [{"member_id": "member-1"},
                                   {"member_id": "member-1"}]},
            "expected": {"members": [{"entity_id": "entity-1",
                                      "member_id": "member-1"}]},
        })
    assert duplicate.value.code == "invalid_reference_item"


def test_append_past_the_hard_cap_is_allowed_by_the_route():
    """The route must not invent a stricter rule than `+ Add member` already has.

    Two paths into one durable state with two different limits would be a second
    authority; the drop instead SAYS so immediately, and `nodes/reference_core`
    keeps refusing the render.
    """
    project, scene = _project_with_reference("image")
    scene.reference_lane_recipes[0] = ReferenceLaneRecipe(
        lane_id="lane-a", media_kind="image", recipe={"hard": {"max_members": 1}})
    second_asset = Asset(asset_id="asset-2", name="Two", asset_type="image",
                         path="media/two.image")
    project.assets.append(second_asset)
    project.references[0].members.append(
        ReferenceMember(member_id="member-2", asset_id="asset-2"))
    scene.reference_items = [ReferenceItem(
        reference_item_id="item", lane_index=0, start_frame=0, end_frame=-1,
        members=[{"entity_id": "entity-1", "member_id": "member-1"}])]

    item = routes._apply_update_reference_item(project, scene, {
        "reference_item_id": "item",
        "fields": {"members": [{"member_id": "member-1"}, {"member_id": "member-2"}]},
        "expected": {"members": [{"entity_id": "entity-1", "member_id": "member-1"}]},
    })
    assert [value["member_id"] for value in item.members] == ["member-1", "member-2"]


def test_member_population_compatibility_matches_between_python_and_the_browser():
    """The mirror is compared against the EXTRACTED rule, not a reimplementation."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for population parity coverage")
    module_url = (ROOT / "web" / "js" / "reference_lane_identity.js").as_uri()
    cases = [
        {"population": population, "mediaKind": media_kind,
         "assetType": asset_type, "hasAudio": has_audio}
        for population in ("", "pictures", "videos", "standalone_audios", "unknown")
        for media_kind in ("image", "video", "audio")
        for asset_type in ("image", "video", "audio", "")
        for has_audio in (True, False)
    ]
    script = """
const mod = await import(%s);
const cases = %s;
console.log(JSON.stringify(cases.map((c) => mod.memberPopulationCompatible(
  c.population, c.mediaKind, c.assetType, { hasAudio: c.hasAudio }))));
""" % (json.dumps(module_url), json.dumps(cases))
    browser = json.loads(subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True).stdout)
    python = [routes.member_population_compatible(
        case["population"], case["mediaKind"], case["assetType"],
        has_audio=case["hasAudio"]) for case in cases]
    assert browser == python
    # The parity must be non-trivial in both directions.
    assert any(python) and not all(python)
def test_new_reference_ops_reach_their_handlers_through_the_dispatch():
    """The field names the client actually sends, checked end to end.

    Every other test in this file calls `routes._move_media_lane` /
    `routes._apply_split_reference_item` directly, so a typo in the dispatch —
    or in `from_index` / `to_index` / `reference_item_id` / `frame` — would be
    invisible. This is the only coverage of the operation envelope itself.
    """
    project, scene = _split_scene()
    # `_project_with_reference` already gives this scene two lanes; name the
    # second one so the move's identity guard has something exact to compare.
    scene.reference_lane_recipes[1] = ReferenceLaneRecipe(
        lane_id="lane-b", media_kind="image")

    split = routes._apply_scene_mutation_operation(project, scene, {
        "type": "split_reference_item",
        "reference_item_id": "item",
        "frame": 50,
        "expected": _split_expected(scene),
    })
    assert split["type"] == "split_reference_item"
    assert split["reference_item_id"] == "item"
    assert split["right_reference_item_id"] != "item"
    assert split["bound_attachment_count"] == 0
    assert {item.reference_item_id for item in scene.reference_items} == {
        "item", split["right_reference_item_id"]}

    move = routes._apply_scene_mutation_operation(project, scene, {
        "type": "move_lane",
        "lane_type": "reference",
        "from_index": 0,
        "to_index": 1,
        "expected": {"from_lane_id": "lane-a", "to_lane_id": "lane-b"},
    })
    assert move == {"type": "move_lane", "lane_type": "reference",
                    "from_index": 0, "to_index": 1}
    assert [recipe.lane_id for recipe in scene.reference_lane_recipes] == [
        "lane-b", "lane-a"]
    assert {item.lane_index for item in scene.reference_items} == {1}

    # An unknown lane type reaches the same refusal through the envelope.
    with pytest.raises(routes.ProjectMutationRequestError):
        routes._apply_scene_mutation_operation(project, scene, {
            "type": "move_lane", "lane_type": "video",
            "from_index": 0, "to_index": 1, "expected": {},
        })


def test_move_lane_refuses_a_family_that_carries_no_durable_lane_identity():
    """The identity guard must not live inside the recipe branch.

    `lane_movable` is the documented gate for "designed and tested", but nothing
    forces a durable identity to travel with that opt-in: a future family
    without `recipe_attr` would otherwise inherit a swap that accepts `{}` and
    reorders durable state on a bare index.
    """
    scene = _movable_scene()
    with pytest.raises(routes.ProjectMutationRequestError) as missing:
        routes._move_media_lane(scene, "reference", 0, 1, "not-a-dict")
    assert missing.value.code == "identity_mismatch"

    # A stored blank id must not match a blank `expected`.
    scene.reference_lane_recipes[0].lane_id = ""
    scene.reference_lane_recipes[1].lane_id = ""
    with pytest.raises(routes.ProjectMutationRequestError) as blank:
        routes._move_media_lane(scene, "reference", 0, 1,
                                {"from_lane_id": "", "to_lane_id": ""})
    assert blank.value.code == "identity_mismatch"
    assert [recipe.recipe_id for recipe in scene.reference_lane_recipes] == ["a", "b", "c"]


def test_splitting_can_raise_an_item_over_the_reference_frame_threshold():
    """A consequence of splitting, disclosed rather than prevented.

    `resolve_effective_references` scores coverage as
    `overlap / (item_end - item_start)`, so halving an item's own span doubles
    its coverage of the same window. An item the threshold used to drop can
    start applying after a split. Inherent to dividing a scope; pinned so it is
    a known property rather than a surprise in a render.
    """
    _, scene = _split_scene(end_frame=100)
    scene.duration_frames = 100
    # `_split_scene` mutes its item to prove the split copies that flag; the
    # resolver skips muted items outright, so unmute it here.
    scene.reference_items[0].muted = False
    before = resolve_effective_references(
        reference_items=scene.reference_items, lane_count=1,
        scene_duration=100, window_start=0, window_end=30,
        lane_configs=scene.reference_lane_configs, frame_threshold_pct=50)
    assert before[0] is None, "30 of 90 frames is under a 50% threshold"

    routes._apply_split_reference_item(scene, {
        "reference_item_id": "item", "frame": 40,
        "expected": _split_expected(scene)})
    after = resolve_effective_references(
        reference_items=scene.reference_items, lane_count=1,
        scene_duration=100, window_start=0, window_end=30,
        lane_configs=scene.reference_lane_configs, frame_threshold_pct=50)
    assert after[0] is not None, "the left half now covers the window"
    assert after[0]["item"].reference_item_id == "item"


@pytest.mark.parametrize("expected", [{"lane_id": "wrong"}, {}, None, "lane-a", {"lane_id": 1}])
def test_lane_config_identity_refusal_precedes_field_writes(expected):
    scene = _movable_scene()
    before = scene.to_dict()
    with pytest.raises(routes.ProjectMutationRequestError) as caught:
        routes._apply_lane_config(scene, {
            "lane_type": "reference", "lane_index": 0, "expected": expected,
            "fields": {"name": "Wrong target", "locked": True, "hidden": True,
                       "reference_recipe": {"lane_id": "replacement"}},
        })
    assert caught.value.status == 409
    assert caught.value.code == "identity_mismatch"
    assert scene.to_dict() == before


@pytest.mark.parametrize("with_expected", [True, False])
def test_lane_config_stored_id_wins_over_client_minted_recipe(with_expected):
    scene = _movable_scene()
    lane_id = scene.reference_lane_recipes[0].lane_id
    op = {"lane_type": "reference", "lane_index": 0,
          "fields": {"name": "Renamed", "locked": True, "hidden": True,
                     "reference_recipe": {"lane_id": "client-minted", "media_kind": "image"}}}
    if with_expected:
        op["expected"] = {"lane_id": lane_id}
    routes._apply_lane_config(scene, op)
    assert scene.reference_lane_recipes[0].lane_id == lane_id
    assert scene.reference_lane_configs[0].name == "Renamed"
    assert scene.reference_lane_configs[0].locked
    assert scene.reference_lane_configs[0].hidden


def test_lane_config_bootstraps_recipe_less_default_and_same_batch_appended_lane():
    scene = Scene(scene_id="bootstrap", reference_lane_count=1, reference_lane_recipes=[])
    project = TimelineProject(project_id="project", scenes=[scene])
    routes._apply_scene_mutation_operation(project, scene, {
        "type": "update_lane_config", "lane_type": "reference", "lane_index": 0,
        "fields": {"name": "Default rename"},
    })
    first_id = scene.reference_lane_recipes[0].lane_id
    assert first_id
    assert scene.reference_lane_configs[0].name == "Default rename"
    routes._apply_scene_mutation_operation(project, scene, {
        "type": "set_lane_count", "lane_type": "reference", "count": 2,
    })
    second_id = scene.reference_lane_recipes[1].lane_id
    routes._apply_scene_mutation_operation(project, scene, {
        "type": "update_lane_config", "lane_type": "reference", "lane_index": 1,
        "fields": {"name": "Appended", "reference_recipe": {"lane_id": "client-minted"}},
    })
    assert scene.reference_lane_configs[1].name == "Appended"
    assert scene.reference_lane_recipes[1].lane_id == second_id != first_id


def test_lane_config_positional_family_keeps_writing_without_recipe_identity():
    scene = Scene(scene_id="scene", video_lane_count=1)
    routes._apply_lane_config(scene, {
        "lane_type": "video", "lane_index": 0, "fields": {"name": "Video", "locked": True},
    })
    assert scene.video_lane_configs[0].name == "Video"
    assert scene.video_lane_configs[0].locked


@pytest.mark.parametrize("raw_recipes", [[], [{"media_kind": "image"}], [{"lane_id": "", "media_kind": "image"}]])
def test_loaded_bootstrap_lane_identity_survives_get_then_mutation(raw_recipes):
    import copy
    raw = Scene(scene_id="bootstrap", reference_lane_count=2).to_dict()
    raw["reference_lane_recipes"] = raw_recipes
    before = copy.deepcopy(raw)
    read = Scene.from_dict(raw)
    write = Scene.from_dict(raw)
    assert [r.lane_id for r in read.reference_lane_recipes] == [r.lane_id for r in write.reference_lane_recipes]
    assert len({r.lane_id for r in read.reference_lane_recipes}) == 2
    routes._apply_lane_config(write, {
        "lane_type": "reference", "lane_index": 0,
        "expected": {"lane_id": read.reference_lane_recipes[0].lane_id},
        "fields": {"name": "First durable edit", "reference_recipe": {"lane_id": "client-draft"}},
    })
    assert write.reference_lane_configs[0].name == "First durable edit"
    round_trip = Scene.from_dict(write.to_dict())
    assert round_trip.reference_lane_recipes[0].lane_id == read.reference_lane_recipes[0].lane_id
    assert raw == before, "read repair must not mutate the saved input"


def test_loaded_reference_lanes_preserve_authored_ids_while_padding_missing_recipes():
    raw = Scene(scene_id="existing", reference_lane_count=3).to_dict()
    raw["reference_lane_recipes"] = [{"lane_id": "authored-b"}, {"lane_id": "authored-a"}]
    scene = Scene.from_dict(raw)
    assert [r.lane_id for r in scene.reference_lane_recipes[:2]] == ["authored-b", "authored-a"]
    assert scene.reference_lane_recipes[2].lane_id not in {"authored-b", "authored-a", ""}
