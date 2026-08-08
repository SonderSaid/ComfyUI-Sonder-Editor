import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import routes
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
    """media_kind is a hard lane property: only a never-configured lane adopts it."""
    source = (ROOT / "web" / "js" / "editor_widget.js").read_text(encoding="utf-8")
    can_use = source.split("const canUse = (entry) => {", 1)[1].split("};", 1)[0]
    assert "if (recipe.media_kind === mediaKind) return true;" in can_use
    assert "return !occupied && this._isUnconfiguredReferenceLaneRecipe(recipe);" in can_use
    unconfigured = source.split("_isUnconfiguredReferenceLaneRecipe(recipe) {", 1)[1].split("\n    }", 1)[0]
    assert 'value.media_kind || "image") === "image"' in unconfigured
    assert '!String(value.recipe_id || "")' in unconfigured
    assert '!Object.keys(value.recipe || {}).length' in unconfigured


def test_reference_bridge_shape_tracks_project_writes_and_recipe_liveness():
    """Library/timeline mutations move the durable version; slots follow it."""
    bridge = (ROOT / "web" / "js" / "reference_bridge.js").read_text(encoding="utf-8")
    client = (ROOT / "web" / "js" / "api_client.js").read_text(encoding="utf-8")
    assert 'import { onProjectVersionChanged } from "./api_client.js";' in bridge
    assert "onProjectVersionChanged(refreshAllBridges);" in bridge
    assert "lane.image_slot_count" in bridge
    assert "lane.audio_slot_count" in bridge
    assert "lane.prompt_slot_count" in bridge
    assert "BRIDGES.has(nodeType(target))" in bridge
    assert "controller.whenProjectReady(() => refreshShape(node));" in bridge
    assert "Refresh reference slots" in bridge
    # Every "we don't know" path resolves to the full shape, never a subset: an
    # unwired selector, an unresolved project, a missing lane and a failed fetch.
    assert bridge.count("return FULL_SHAPE;") == 4
    assert "applyReferenceBridgeShape(node, FULL_SHAPE)" in bridge
    assert "export function onProjectVersionChanged(callback)" in client
    assert "if (next !== current) emitProjectVersionChanged(normalizedProjectId, next);" in client


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


def test_legacy_live_outputs_migrate_in_all_four_stores_and_remain_writable():
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
    assert scene.reference_lane_recipes[0].recipe["hard"]["live_outputs"] == ["image_slots"]

    job = GenerationJob.from_dict({"reference_lane_recipes": [legacy_wrapper]})
    assert job.reference_lane_recipes[0]["recipe"]["hard"]["live_outputs"] == ["image_slots"]

    project = TimelineProject.from_dict({
        "project_id": "project",
        "reference_recipes": [{
            "id": "custom:legacy", "name": "Legacy", "media_kind": "audio",
            "hard": {"assembly": "audio", "live_outputs": ["reference_audio"]}, "soft": {},
        }],
    })
    assert project.reference_recipes[0]["hard"]["live_outputs"] == ["audio_slots"]
    normalized = routes._normalize_reference_recipe_section(
        {"assembly": "slots", "live_outputs": ["slots", "reference_audio", "reference_idx"]},
        "hard",
    )
    assert normalized["live_outputs"] == ["image_slots", "audio_slots"]

    # A retired-only declaration fails open instead of becoming "drives nothing".
    empty = routes._normalize_reference_recipe_section({"live_outputs": ["reference_idx", "context"]}, "hard")
    assert "live_outputs" not in empty
    assert reference_live_outputs(empty) == set(REFERENCE_OUTPUT_NAMES)

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
