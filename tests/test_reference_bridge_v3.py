import importlib
import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

from server import minimax_h3, prompt_context
from server.timeline_state import (
    MINIMAX_H3_REFERENCE_RECIPE_PRESETS,
    REFERENCE_RECIPE_PRESETS,
    Asset,
    GenerationJob,
    LaneConfig,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
    Scene,
    PromptSection,
    TimelineProject,
)


ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "reference_bridge_testpkg"


class _Value:
    def __init__(self, id=None, **kwargs):
        self.id = id
        for key, value in kwargs.items():
            setattr(self, key, value)


class _Schema(_Value):
    pass


class _NodeOutput:
    def __init__(self, *values):
        self.values = values


class _Node:
    @classmethod
    def GET_SCHEMA(cls):
        return cls.define_schema()


class _Type:
    Input = _Value
    Output = _Value


def _install_io(monkeypatch):
    def typed(name):
        return types.SimpleNamespace(
            Input=lambda id=None, **kwargs: _Value(
                id=id, socket_type=name, **kwargs),
            Output=lambda id=None, **kwargs: _Value(
                id=id, socket_type=name, **kwargs),
        )
    io = types.SimpleNamespace(
        ComfyNode=_Node,
        Schema=_Schema,
        Custom=lambda name: typed(name),
        Int=typed("INT"),
        Float=typed("FLOAT"),
        String=typed("STRING"),
        Combo=typed("COMBO"),
        Image=typed("IMAGE"),
        Audio=typed("AUDIO"),
        NumberDisplay=types.SimpleNamespace(number="number"),
        NodeOutput=_NodeOutput,
    )
    comfy = types.ModuleType("comfy_api")
    version = types.ModuleType("comfy_api.v0_0_2")
    version.io = io
    comfy.v0_0_2 = version
    monkeypatch.setitem(sys.modules, "comfy_api", comfy)
    monkeypatch.setitem(sys.modules, "comfy_api.v0_0_2", version)


def _import_module(monkeypatch, name):
    if TEST_PACKAGE not in sys.modules:
        package = types.ModuleType(TEST_PACKAGE)
        package.__path__ = [str(ROOT)]
        monkeypatch.setitem(sys.modules, TEST_PACKAGE, package)
    module_name = f"{TEST_PACKAGE}.nodes.{name}"
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


def _project(tmp_path, recipe, *, hidden=False):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    image_path = media_dir / "subject.png"
    image = np.zeros((24, 40, 3), dtype=np.uint8)
    image[:, :, 2] = 255
    assert cv2.imwrite(str(image_path), image)
    asset = Asset(asset_id="asset", name="Red", asset_type="image", path="media/subject.png", width=40, height=24)
    member = ReferenceMember(member_id="member", asset_id="asset", prompt="red subject")
    reference = ReferenceEntity(reference_id="entity", name="Hero", members=[member])
    item = ReferenceItem(
        reference_item_id="item", lane_index=0, start_frame=10, end_frame=-1,
        members=[{"entity_id": "entity", "member_id": "member"}],
    )
    scene = Scene(
        scene_id="scene", duration_frames=60, width=64, height=48,
        reference_lane_count=1,
        reference_lane_configs=[LaneConfig(hidden=hidden)],
        reference_lane_recipes=[recipe],
        reference_items=[item],
    )
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="project", resolution=(64, 48),
        scenes=[scene], assets=[asset], references=[reference],
    )
    project._execution_context = {"scene_id": "scene", "context_start": 12, "context_end": 30}
    return project


def _preset(recipe_id: str) -> ReferenceLaneRecipe:
    definition = next(row for row in REFERENCE_RECIPE_PRESETS if row["id"] == recipe_id)
    return ReferenceLaneRecipe(
        media_kind=definition["media_kind"],
        recipe_id=definition["id"],
        recipe={"name": definition["name"], "hard": dict(definition["hard"]), "soft": dict(definition["soft"])},
    )


def _h3_preset(recipe_id: str) -> ReferenceLaneRecipe:
    definition = next(
        row for row in MINIMAX_H3_REFERENCE_RECIPE_PRESETS
        if row["id"] == recipe_id)
    return ReferenceLaneRecipe(
        media_kind=definition["media_kind"],
        recipe_id=definition["id"],
        recipe={"name": definition["name"], "hard": dict(definition["hard"]),
                "soft": dict(definition["soft"])},
    )


def _multi_member_project(tmp_path, recipe, colors, *, tags=()):
    """A staged item with one distinctly coloured member per entry in ``colors``."""
    media_dir = tmp_path / "media"
    media_dir.mkdir(parents=True)
    assets, references, members = [], [], []
    for index, color in enumerate(colors):
        path = media_dir / f"subject{index}.png"
        image = np.zeros((24, 40, 3), dtype=np.uint8)
        image[:, :] = color[::-1]  # cv2 writes BGR
        assert cv2.imwrite(str(path), image)
        assets.append(Asset(
            asset_id=f"asset{index}", name=f"Subject {index}", asset_type="image",
            path=f"media/subject{index}.png", width=40, height=24,
        ))
        member = ReferenceMember(
            member_id=f"member{index}", asset_id=f"asset{index}",
            prompt=f"subject {index}", tags=list(tags[index]) if index < len(tags) else [],
        )
        references.append(ReferenceEntity(
            reference_id=f"entity{index}", name=f"Hero {index}", members=[member],
            reference_class="context" if "sonder:location" in member.tags else "subject",
        ))
        members.append({"entity_id": f"entity{index}", "member_id": f"member{index}"})
    scene = Scene(
        scene_id="scene", duration_frames=60, width=64, height=48,
        reference_lane_count=1,
        reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[recipe],
        reference_items=[ReferenceItem(
            reference_item_id="item", lane_index=0, start_frame=10, end_frame=-1, members=members,
        )],
    )
    project = TimelineProject(
        project_dir=str(tmp_path), project_id="project", resolution=(64, 48),
        scenes=[scene], assets=assets, references=references,
    )
    project._execution_context = {"scene_id": "scene", "context_start": 12, "context_end": 30}
    return project


def test_msr_places_each_reference_on_a_contiguous_grid_segment_from_index_zero(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    project = _multi_member_project(tmp_path, _preset("sonder:ltx_msr"), [(255, 0, 0), (0, 255, 0), (0, 0, 255)])
    frames = core.decode_reference_images(core.resolve_reference_set(project, 0))[0]

    # 3 refs on the 8k+1 LTX grid is the 25-frame bucket, split into contiguous
    # segments: the first ref owns index 0 and the last absorbs the +1 tail.
    assert frames.shape[0] == 25
    channel = frames.reshape(25, -1, 3).mean(dim=1).argmax(dim=1).tolist()
    assert channel == [0] * 8 + [1] * 8 + [2] * 9
    # No frame is left black between references.
    assert float(frames.reshape(25, -1).max(dim=1).values.min()) > 0.5


def test_msr_places_a_context_member_in_the_final_temporal_segment(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    project = _multi_member_project(
        tmp_path, _preset("sonder:ltx_msr"), [(255, 0, 0), (0, 0, 255)],
        tags=([], ["sonder:location"]),
    )
    frames = core.decode_reference_images(core.resolve_reference_set(project, 0))[0]
    # Sonder deliberately keeps the context member in the sequence and sorts it
    # to the tail, overriding the surveyed separate background socket.
    assert frames.shape[0] == 17
    channel = frames.reshape(17, -1, 3).mean(dim=1).argmax(dim=1).tolist()
    assert channel == [0] * 8 + [2] * 9


def test_ingredients_loops_the_sheet_without_constraining_the_render_window(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    project = _multi_member_project(tmp_path, _preset("sonder:ltx_ingredients"), [(255, 0, 0), (0, 255, 0)])
    # The render window is 18 frames — far under the 121-frame reference
    # sequence. That must not refuse the render.
    frames = core.decode_reference_images(core.resolve_reference_set(project, 0))[0]
    assert tuple(frames.shape) == (121, 48, 64, 3)
    assert bool((frames[0] == frames[120]).all())


def test_vace_composites_an_equal_width_strip_on_floored_divisible_16_geometry(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    wide = np.zeros((10, 40, 3), dtype=np.uint8)
    wide[:, :, 0] = 255
    narrow = np.zeros((20, 10, 3), dtype=np.uint8)
    narrow[:, :, 1] = 255
    strip = core._strip([wide, narrow], 64, 64, "white")
    assert tuple(strip.shape) == (1, 64, 64, 3)
    assert np.allclose(strip[0, 0, 0].numpy(), 1.0)  # white padding, not black
    # Members stack vertically at one common width in staged order.
    assert strip[0, 3, 32, 0] > strip[0, 3, 32, 1]
    assert strip[0, 40, 32, 1] > strip[0, 40, 32, 0]

    hard = dict(next(row for row in REFERENCE_RECIPE_PRESETS if row["id"] == "sonder:wan_vace")["hard"])
    # 1080 floors to 1072; rounding would have exceeded the requested output.
    assert core._member_geometry(wide, hard, 1920, 1080, 2) == (1920, 1072)


def test_best_face_id_uses_the_bust_size_for_a_lone_member(monkeypatch):
    core = _import_module(monkeypatch, "reference_core")
    hard = dict(next(row for row in REFERENCE_RECIPE_PRESETS if row["id"] == "sonder:ltx_best_face_id")["hard"])
    frame = np.zeros((24, 40, 3), dtype=np.uint8)
    assert core._member_geometry(frame, hard, 64, 48, 1) == (460, 406)
    assert core._member_geometry(frame, hard, 64, 48, 4) == (1536, 1024)


def test_h3_generic_recipe_geometry_and_frame_grid_match_model_contract(monkeypatch):
    core = _import_module(monkeypatch, "reference_core")
    picture = _h3_preset("sonder:minimax_h3_picture").recipe["hard"]
    assert core._member_geometry(
        np.zeros((1080, 1920, 3), dtype=np.uint8), picture, 64, 48,
    ) == (1920, 1056)
    assert core._member_geometry(
        np.zeros((4000, 3000, 3), dtype=np.uint8), picture, 64, 48,
    ) == (2048, 2720)

    video = _h3_preset("sonder:minimax_h3_video").recipe["hard"]
    assert [core._snap_span_frame_count(count, video) for count in (5, 24, 120)] == [5, 22, 107]


def test_h3_video_recipe_is_served_by_generic_image_bridge(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    recipe = _h3_preset("sonder:minimax_h3_video")
    assert recipe.media_kind == "image"
    project = _project(tmp_path, recipe)
    asset = project.assets[0]
    asset.asset_type = "video"
    asset.fps = 30.0
    asset.frame_count = 30
    asset.duration_sec = 1.0
    project.references[0].members[0].source_end_sec = 1.0
    monkeypatch.setattr(
        core, "resolve_existing_project_path",
        lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    monkeypatch.setattr(core, "resolve_source_color_interpretation",
                        lambda *_args, **_kwargs: None)
    monkeypatch.setattr(core, "decode_video_range", lambda _path, start, end, **_kwargs: (
        np.full((24, 40, 3), index % 255, dtype=np.uint8)
        for index in range(start, end)))

    slots = core.decode_reference_images(core.resolve_reference_set(project, 0))
    assert len(slots) == 16
    assert tuple(slots[0].shape) == (22, 32, 32, 3)
    assert float(slots[1].abs().max()) == 0.0


def test_homogeneous_bridges_emit_assembled_and_per_member_payloads(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    project = _multi_member_project(tmp_path, _preset("sonder:wan_vace"), [(255, 0, 0), (0, 255, 0)])
    resolved = core.resolve_reference_set(project, 0)
    images = core.decode_reference_images(resolved)
    prompts = core.decode_reference_prompts(resolved)
    assert len(images) == 16 and len(prompts) == 18
    assert float(images[0].abs().max()) > 0.0
    assert all(float(slot.abs().max()) == 0.0 for slot in images[1:])
    assert prompts[0] and prompts[1]
    assert prompts[2] and prompts[3]
    assert all(slot == "" for slot in prompts[4:])

    bernini = _multi_member_project(tmp_path / "b", _preset("sonder:wan_bernini"), [(255, 0, 0), (0, 255, 0)])
    slots = core.decode_reference_images(core.resolve_reference_set(bernini, 0))
    assert float(slots[0].abs().max()) > 0.0
    assert float(slots[1].abs().max()) > 0.0
    assert float(slots[2].abs().max()) == 0.0


def test_selector_is_effective_in_window_and_cache_identity_tracks_window(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    recipe = ReferenceLaneRecipe(recipe_id="batch", recipe={"hard": {"assembly": "batch", "max_members": 4}})
    project = _project(tmp_path, recipe)
    selected = core.resolve_reference_set(project, 0)
    assert selected["has_reference"] == 1
    assert selected["item"]["reference_item_id"] == "item"
    assert core.resolve_reference_set(project, 1)["has_reference"] == 0
    first = core.reference_fingerprint(project, 0)
    project._execution_context["context_start"] = 31
    second = core.reference_fingerprint(project, 0)
    assert first != second
    project.modified_at = "library-revision-2"
    third = core.reference_fingerprint(project, 0)
    assert second != third
    assert np.isnan(core.reference_fingerprint(None, 0))


def test_selector_uses_frozen_explicit_snapshot_end_after_scene_shrinks(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    project = _project(tmp_path, ReferenceLaneRecipe())
    project.fps = 30.0
    project.generation_queue = [GenerationJob(
        job_id="queued",
        scene_id="scene",
        params={"snapshot_version": 1},
        scene_fps=0.0,
        reference_lane_count=1,
        reference_lane_configs=[LaneConfig().to_dict()],
        reference_lane_recipes=[ReferenceLaneRecipe().to_dict()],
        reference_item_snapshots=[{
            "reference_item_id": "frozen",
            "lane_index": 0,
            "start_frame": 10,
            "end_frame": 60,
            "members": [{"entity_id": "entity", "member_id": "member"}],
        }],
        reference_input_snapshots=[
            {"kind": "reference", "value": project.references[0].to_dict()},
            {"kind": "asset", "value": project.assets[0].to_dict()},
        ],
    )]
    project.scenes[0].duration_frames = 20
    project._execution_context.update({
        "queue_job_ref_id": "queued",
        "context_start": 30,
        "context_end": 40,
    })
    selected = core.resolve_reference_set(project, 0)
    assert selected["source"] == "snapshot"
    assert selected["item"]["reference_item_id"] == "frozen"
    assert selected["pegs"]["fps"] == 30.0
    assert core._reference_frame_rate(core._reference_decode_context(selected, "image")) == 30.0


def test_snapshot_selector_uses_frozen_library_catalog_after_live_edits(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    monkeypatch.setattr(core, "resolve_existing_project_path",
                        lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    recipe = _preset("sonder:ltx_msr")
    project = _project(tmp_path, recipe)
    project.references[0].members[0].name = "portrait"
    frozen_reference = project.references[0].to_dict()
    frozen_asset = project.assets[0].to_dict()
    project.generation_queue = [GenerationJob(
        job_id="queued", scene_id="scene", params={"snapshot_version": 1},
        reference_lane_count=1,
        reference_lane_configs=[LaneConfig().to_dict()],
        reference_lane_recipes=[recipe.to_dict()],
        reference_item_snapshots=[project.scenes[0].reference_items[0].to_dict()],
        reference_input_snapshots=[
            {"kind": "reference", "value": frozen_reference},
            {"kind": "asset", "value": frozen_asset},
        ],
    )]
    project._execution_context["queue_job_ref_id"] = "queued"

    project.references[0].name = "CHANGED"
    project.references[0].members[0].name = "changed"
    project.references[0].members[0].prompt = "changed prompt"
    project.assets[0].path = "media/missing.png"

    selected = core.resolve_reference_set(project, 0)
    context = core._reference_decode_context(selected, "image")
    assert context["records"][0]["reference"].name == "Hero"
    assert context["records"][0]["member"].name == "portrait"
    assert context["records"][0]["member"].prompt == "red subject"
    assert context["records"][0]["asset"].path == "media/subject.png"
    assert core.decode_reference_prompts(selected)[1] == "Hero_portrait"
    assert float(core.decode_reference_images(selected)[0].abs().max()) > 0.0


def test_selector_excludes_hidden_lane(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    project = _project(tmp_path, ReferenceLaneRecipe(), hidden=True)
    assert core.resolve_reference_set(project, 0)["has_reference"] == 0


def test_bridge_absent_fallback_and_present_slot_assembly(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    project = _project(tmp_path, ReferenceLaneRecipe(
        recipe_id="slots",
        recipe={"soft": {"prompt_tokens": "image{index}"}, "hard": {
            "assembly": "slots", "max_members": 8, "native_aspect": True,
            "long_edge_max": 848, "dimension_multiple": 16,
        }},
    ))
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    absent = core.decode_reference_images({"has_reference": 0, "width": 64, "height": 48})
    assert len(absent) == 16
    assert tuple(absent[0].shape) == (1, 48, 64, 3)

    resolved = core.resolve_reference_set(project, 0)
    present = core.decode_reference_images(resolved)
    prompts = core.decode_reference_prompts(resolved)
    assert len(present) == 16 and len(prompts) == 18
    assert resolved["strength"] == 1.0
    assert prompts[0] == "image0: red subject"
    assert prompts[1] == "Hero"
    assert present[0].shape[0] == 1
    assert tuple(present[1].shape) == (1, 48, 64, 3)


@pytest.mark.parametrize("assembly", ["slots", "sheet", "batch", "temporal"])
def test_image_bridge_nothing_preserves_live_payload_and_omits_every_dead_slot(
        monkeypatch, tmp_path, assembly):
    core = _import_module(monkeypatch, "reference_core")
    hard = {
        "assembly": assembly,
        "max_members": 4,
        "live_outputs": ["image_slots"],
        "output_size": "scene",
    }
    if assembly == "temporal":
        hard.update({"frame_step": 8, "frame_offset": 1, "allowed_frame_counts": [9, 17]})
    project = _multi_member_project(
        tmp_path,
        ReferenceLaneRecipe(recipe={"hard": hard}),
        [(255, 0, 0)],
    )
    monkeypatch.setattr(
        core,
        "resolve_existing_project_path",
        lambda project, path, **_kwargs: str(Path(project.project_dir) / path),
    )

    values = core.decode_reference_images(core.resolve_reference_set(project, 0), "nothing")
    assert len(values) == 16
    assert float(values[0].abs().max()) > 0.0
    assert values[1:] == (None,) * 15


def test_image_bridge_nothing_covers_absent_and_dead_blocks_and_unknown_policy(
        monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    absent = {"has_reference": 0, "width": 64, "height": 48}
    assert core.decode_reference_images(absent, "nothing") == (None,) * 16

    project = _project(tmp_path, ReferenceLaneRecipe(recipe={"hard": {
        "assembly": "slots", "max_members": 4, "live_outputs": [],
    }}))
    dead = core.decode_reference_images(core.resolve_reference_set(project, 0), "nothing")
    assert dead == (None,) * 16

    tolerant = core.decode_reference_images(absent, "unrecognised")
    assert len(tolerant) == 16
    assert all(tuple(value.shape) == (1, 48, 64, 3) for value in tolerant)


def test_audio_bridge_nothing_preserves_live_payload_and_omits_fallbacks(
        monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")

    def audio_project(path, live_outputs):
        project = _multi_member_project(
            path,
            ReferenceLaneRecipe(
                media_kind="audio",
                recipe={"hard": {
                    "assembly": "audio",
                    "max_members": 16,
                    "live_outputs": live_outputs,
                }},
            ),
            [(255, 0, 0)],
        )
        for asset in project.assets:
            asset.asset_type = "audio"
        return project

    monkeypatch.setattr(core, "_audio_output", lambda record: {
        "waveform": record["member"].member_id,
        "sample_rate": 44100,
    })
    live_project = audio_project(tmp_path / "live", ["audio_slots"])
    live = core.decode_reference_audios(core.resolve_reference_set(live_project, 0), "nothing")
    assert live[0] == {"waveform": "member0", "sample_rate": 44100}
    assert live[1:] == (None,) * 15

    absent = {"has_reference": 0, "width": 64, "height": 48}
    assert core.decode_reference_audios(absent, "nothing") == (None,) * 16
    dead_project = audio_project(tmp_path / "dead", [])
    assert core.decode_reference_audios(
        core.resolve_reference_set(dead_project, 0), "nothing") == (None,) * 16

    tolerant = core.decode_reference_audios(absent, "unrecognised")
    assert len(tolerant) == 16
    assert all(value["sample_rate"] == 44100 for value in tolerant)
    assert all(tuple(value["waveform"].shape) == (1, 2, 44100) for value in tolerant)
    assert tolerant[0] is not tolerant[1]
    assert tolerant[0]["waveform"] is not tolerant[1]["waveform"]
    tolerant[0]["waveform"][0, 0, 0] = 1.0
    assert float(tolerant[1]["waveform"][0, 0, 0]) == 0.0


def test_prompt_bridge_member_slot_agrees_with_h3_subject_definition(
        monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    recipe = ReferenceLaneRecipe(
        lane_id="pictures", media_kind="image", recipe={
            "soft": {
                "compatible_profiles": ["minimax_h3_ref@1"],
                "physical_population": "pictures",
                "exposed_capabilities": ["definitions"],
                "prompt_tokens": (
                    "<Subject {subject_n}> is {prompt} from <Picture {picture_n}>"),
            },
            "hard": {"assembly": "slots", "max_members": 9},
        })
    project = _project(tmp_path, recipe)
    unit = prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "subject", "name": "Hero", "definition": "",
        "sources": [{"entity_id": "entity", "member_id": "member"}],
    })
    project.prompt_semantic_units = [unit]

    reference_set = core.resolve_reference_set(project, 0)
    p01 = core.decode_reference_prompts(reference_set)[2]
    setup = minimax_h3.resolve_setup(
        setup={"mode": "reference", "picture_lane_ids": ["pictures"]},
        reference_items=project.scenes[0].reference_items,
        lane_recipes=project.scenes[0].reference_lane_recipes,
        lane_configs=project.scenes[0].reference_lane_configs,
        lane_count=1, scene_duration=60, window_start=12, window_end=30,
        references=project.references, assets=project.assets,
        semantic_units=[unit])
    attachment = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["subject"]},
        "capabilities": [{
            "capability_id": "definitions", "kind": "definitions",
            "channel_key": "subject_definitions", "placement": "section_prefix",
        }],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[PromptSection(
            start_frame=0, end_frame=60,
            channels={"detailed_description": "move"},
            attachments=[attachment])],
        window_start=12, window_end=30, fps=24, template="minimax_h3_ref",
        context={**setup, "semantic_units": [unit]})

    assert p01 == "<Subject 1> is red subject from <Picture 1>"
    assert compiled["channels"]["subject_definitions"] == p01


def test_sheet_assembly_preserves_recipe_background_padding(monkeypatch):
    core = _import_module(monkeypatch, "reference_core")
    tall = np.zeros((40, 10, 3), dtype=np.uint8)
    tall[:, :, 1] = 127
    sheet = core._sheet([tall], 64, 32, "white")
    assert tuple(sheet.shape) == (1, 32, 64, 3)
    assert np.allclose(sheet[0, 0, 0].numpy(), 1.0)
    assert np.allclose(sheet[0, -1, -1].numpy(), 1.0)
    assert not np.allclose(sheet[0, 16, 32].numpy(), 1.0)


def test_bridge_refuses_silent_member_loss_and_missing_media(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    recipe = ReferenceLaneRecipe(recipe={"hard": {"assembly": "batch", "max_members": 1}})
    project = _project(tmp_path, recipe)
    project.scenes[0].reference_items[0].members.append({"entity_id": "entity", "member_id": "member"})
    ref = core.resolve_reference_set(project, 0)
    with pytest.raises(RuntimeError, match="at most 1"):
        core.decode_reference_images(ref)
    project.scenes[0].reference_items[0].members = [{"entity_id": "entity", "member_id": "missing"}]
    with pytest.raises(RuntimeError, match="no longer exists"):
        core.decode_reference_images(core.resolve_reference_set(project, 0))


def test_v3_schema_freezes_selector_and_homogeneous_bridge_socket_names(monkeypatch):
    _install_io(monkeypatch)
    module = _import_module(monkeypatch, "reference_bridge_v3")
    selector = module.SonderReferenceSelector.define_schema()
    image = module.SonderReferenceImageBridge.define_schema()
    audio = module.SonderReferenceAudioBridge.define_schema()
    prompt = module.SonderReferencePromptBridge.define_schema()
    assert selector.node_id == "SonderReferenceSelector"
    assert selector.category == "Sonder"
    assert [value.id for value in selector.inputs] == ["project", "reference_lane_index"]
    assert [value.display_name for value in selector.outputs] == ["reference_set", "has_reference", "reference_strength"]
    assert [value.socket_type for value in selector.inputs] == ["SONDER_PROJECT", "INT"]
    assert [value.socket_type for value in selector.outputs] == [
        "SONDER_REFERENCE_SET", "INT", "FLOAT"]
    assert image.node_id == "SonderReferenceImageBridge"
    assert audio.node_id == "SonderReferenceAudioBridge"
    assert prompt.node_id == "SonderReferencePromptBridge"
    assert [value.id for value in image.outputs] == [f"r{index:02d}" for index in range(1, 17)]
    assert [value.id for value in audio.outputs] == [f"a{index:02d}" for index in range(1, 17)]
    assert [value.id for value in prompt.outputs] == [
        "reference_prompt", "reference_names", *[f"p{index:02d}" for index in range(1, 17)],
    ]
    assert [value.socket_type for value in image.outputs] == ["IMAGE"] * 16
    assert [value.socket_type for value in audio.outputs] == ["AUDIO"] * 16
    assert [value.socket_type for value in prompt.outputs] == ["STRING"] * 18
    assert [value.id for value in image.inputs] == ["reference_set", "unused_slots"]
    assert [value.id for value in audio.inputs] == ["reference_set", "unused_slots"]
    assert [value.id for value in prompt.inputs] == ["reference_set"]
    for policy in (image.inputs[1], audio.inputs[1]):
        assert policy.socket_type == "COMBO"
        assert policy.options == ["placeholder", "nothing"]
        assert policy.default == "placeholder"
        assert policy.optional is True
        assert policy.tooltip
    assert module.SonderReferenceImageBridge.execute(
        {"has_reference": 0, "width": 64, "height": 48}, "nothing").values == (None,) * 16
    assert module.SonderReferenceAudioBridge.execute(
        {"has_reference": 0, "width": 64, "height": 48}, "nothing").values == (None,) * 16
    assert tuple(module.SonderReferenceImageBridge.execute(
        {"has_reference": 0, "width": 64, "height": 48}).values[0].shape) == (1, 48, 64, 3)
    assert module.SonderReferencePromptBridge.execute({"has_reference": 0}).values == (
        "", "", *("" for _ in range(16)))
    assert all(schema.category == "Sonder" for schema in (selector, image, audio, prompt))
    assert all(callable(getattr(cls, "execute", None)) for cls in (
        module.SonderReferenceSelector, module.SonderReferenceImageBridge,
        module.SonderReferenceAudioBridge, module.SonderReferencePromptBridge))
    assert all(getattr(value, "tooltip", "") for schema in (image, audio, prompt) for value in schema.outputs)
    assert all(getattr(value, "tooltip", "") for value in selector.outputs)


def test_cross_kind_bridge_wiring_fails_with_a_sonder_owned_message(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    project = _project(tmp_path, ReferenceLaneRecipe(media_kind="image", recipe={"hard": {"assembly": "slots"}}))
    ref = core.resolve_reference_set(project, 0)
    with pytest.raises(RuntimeError, match="Audio Bridge cannot decode an image Reference lane"):
        core.decode_reference_audios(ref)


def test_audio_bridge_emits_one_trimmed_member_per_slot(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    project = _multi_member_project(
        tmp_path,
        ReferenceLaneRecipe(
            media_kind="audio",
            recipe={"hard": {"assembly": "audio", "max_members": 16,
                              "live_outputs": ["audio_slots"]}},
        ),
        [(255, 0, 0), (0, 255, 0), (0, 0, 255)],
    )
    for asset in project.assets:
        asset.asset_type = "audio"
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    monkeypatch.setattr(core, "_audio_output", lambda record: {
        "waveform": record["member"].member_id,
        "sample_rate": 44100,
    })
    values = core.decode_reference_audios(core.resolve_reference_set(project, 0))
    assert len(values) == 16
    assert [value["waveform"] for value in values[:3]] == ["member0", "member1", "member2"]
    assert values[3]["waveform"].shape[1] == 2


def test_authored_strength_and_sequence_length_reach_selector_and_temporal_assembly(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    project = _multi_member_project(tmp_path, _preset("sonder:ltx_msr"), [(255, 0, 0), (0, 255, 0)])
    item = project.scenes[0].reference_items[0]
    item.strength = 0.35
    item.sequence_frames = 33
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    resolved = core.resolve_reference_set(project, 0)
    assert resolved["strength"] == pytest.approx(0.35)
    assert core.decode_reference_images(resolved)[0].shape[0] == 33


def test_video_member_span_decodes_and_resamples_to_recipe_rate(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    recipe = ReferenceLaneRecipe(recipe={"hard": {
        "assembly": "batch", "max_members": 4, "output_size": "scene",
        "frame_rate_source": "custom", "frame_rate": 24,
        "live_outputs": ["image_slots"],
    }})
    project = _project(tmp_path, recipe)
    asset = project.assets[0]
    asset.asset_type = "video"
    asset.fps = 30.0
    asset.frame_count = 30
    asset.duration_sec = 1.0
    project.references[0].members[0].source_end_sec = 1.0
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    monkeypatch.setattr(core, "resolve_source_color_interpretation", lambda *_args, **_kwargs: None)

    calls = []
    def fake_range(_path, start, end, **_kwargs):
        calls.append((start, end))
        for index in range(start, end):
            yield np.full((24, 40, 3), index, dtype=np.uint8)

    monkeypatch.setattr(core, "decode_video_range", fake_range)
    frames = core.decode_reference_images(core.resolve_reference_set(project, 0))[0]
    assert calls == [(0, 30)]
    assert frames.shape[0] == 24
    assert int(round(float(frames[0].mean()) * 255)) == 0
    assert int(round(float(frames[-1].mean()) * 255)) == 29


# The pre-redesign `hard` geometry blocks, kept verbatim. The Output size
# rework collapsed three overlapping keys (size_mode / native_aspect /
# dimension_multiple) into two orthogonal ones, and it is only a safe
# re-expression if every preset still resolves the same pixels.
_LEGACY_GEOMETRY = {
    "sonder:ltx_msr": {},
    "sonder:ltx_ingredients": {"size_mode": "output"},
    "sonder:ltx_best_face_id": {"width": 1536, "height": 1024, "single_member_size": [460, 406]},
    "sonder:ltx_id_lora_audio": {},
    "sonder:wan_vace": {"size_mode": "output_divisible_16"},
    "sonder:wan_phantom": {"size_mode": "output"},
    "sonder:wan_scail": {"width": 512, "height": 896},
    "sonder:wan_bernini": {"native_aspect": True, "long_edge_max": 848, "dimension_multiple": 16},
}


def _legacy_member_geometry(core, frame, hard, output_width, output_height, member_count=1):
    if hard.get("native_aspect"):
        height, width = frame.shape[:2]
        long_edge = min(max(width, height), max(16, int(hard.get("long_edge_max", 848))))
        scale = long_edge / max(1, max(width, height))
        multiple = max(1, int(hard.get("dimension_multiple", 16)))
        return (
            core._snap_dimension(width * scale, multiple, ceiling=long_edge),
            core._snap_dimension(height * scale, multiple, ceiling=long_edge),
        )
    single = hard.get("single_member_size")
    if member_count <= 1 and isinstance(single, list) and len(single) >= 2:
        return max(1, int(single[0])), max(1, int(single[1]))
    if int(hard.get("width", 0)) and int(hard.get("height", 0)):
        return int(hard["width"]), int(hard["height"])
    if hard.get("size_mode") == "output_divisible_16":
        return (
            core._snap_dimension(output_width, 16, floor=True),
            core._snap_dimension(output_height, 16, floor=True),
        )
    return output_width, output_height


def test_output_size_redesign_resolves_identical_geometry_for_every_preset(monkeypatch):
    core = _import_module(monkeypatch, "reference_core")
    frames = [np.zeros((h, w, 3), dtype=np.uint8) for h, w in ((512, 512), (1080, 1920), (900, 600), (3000, 4000))]
    outputs = ((960, 576), (1024, 1024), (853, 480))
    checked = 0
    for preset in REFERENCE_RECIPE_PRESETS:
        legacy = _LEGACY_GEOMETRY[preset["id"]]
        for frame in frames:
            for output_width, output_height in outputs:
                for member_count in (1, 3):
                    assert core._member_geometry(
                        frame, preset["hard"], output_width, output_height, member_count,
                    ) == _legacy_member_geometry(
                        core, frame, legacy, output_width, output_height, member_count,
                    ), (preset["id"], frame.shape[:2], (output_width, output_height), member_count)
                    checked += 1
    assert checked == len(REFERENCE_RECIPE_PRESETS) * len(frames) * len(outputs) * 2


def test_output_size_modes_are_mutually_exclusive_in_the_assembler(monkeypatch):
    core = _import_module(monkeypatch, "reference_core")
    frame = np.zeros((900, 600, 3), dtype=np.uint8)
    # `custom` width/height are ignored unless the mode selects them, so a value
    # left behind by a mode switch cannot silently drive the render.
    stale = {"output_size": "scene", "width": 1536, "height": 1024}
    assert core._member_geometry(frame, stale, 960, 576, 3) == (960, 576)
    assert core._member_geometry(frame, {**stale, "output_size": "custom"}, 960, 576, 3) == (1536, 1024)
    # `size_multiple` floors rather than rounds up, on every mode that uses it.
    assert core._member_geometry(frame, {"output_size": "scene", "size_multiple": 16}, 970, 580, 3) == (960, 576)
    assert core._member_geometry(frame, {"output_size": "scene", "size_multiple": 1}, 970, 580, 3) == (970, 580)


def test_pegged_recipe_values_follow_their_source_and_fall_back_when_absent(monkeypatch):
    """A pegged value tracks what the scene actually renders with.

    The authored number stays as the fallback, so a project with no resolved
    constraint degrades to what the user typed rather than to zero.
    """
    core = _import_module(monkeypatch, "reference_core")
    authored = {"frame_step": 8, "frame_offset": 1, "size_multiple": 1, "loop_frames": 121}
    pegged = {
        **authored,
        "frame_grid_source": "template",
        "size_multiple_source": "template",
        "loop_frames_source": "window",
    }
    sources = {
        "frame_constraint": {"step": 4, "offset": 1},
        "dimension_constraint": {"step": 32, "offset": 0},
        "window_frames": 97,
    }

    # Nothing pegged: the authored values survive untouched.
    assert core.resolve_pegged_hard(authored, sources) == authored

    resolved = core.resolve_pegged_hard(pegged, sources)
    assert resolved["frame_step"] == 4 and resolved["frame_offset"] == 1
    assert resolved["size_multiple"] == 32
    assert resolved["loop_frames"] == 97

    # Every source missing: each peg keeps its authored fallback.
    assert core.resolve_pegged_hard(pegged, {}) == pegged
    # A constraint present but degenerate is not a usable peg either.
    assert core.resolve_pegged_hard(pegged, {"frame_constraint": {"step": 0}})["frame_step"] == 8
    assert core.resolve_pegged_hard(pegged, {"window_frames": 0})["loop_frames"] == 121


def test_pegs_reach_the_bridge_and_the_selector_fingerprint(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    project = _multi_member_project(tmp_path, _preset("sonder:ltx_ingredients"), [(255, 0, 0), (0, 255, 0)])
    project.dimension_constraint = {"step": 32, "offset": 0}
    project.frame_constraint = {"step": 4, "offset": 1}

    resolved = core.resolve_reference_set(project, 0)
    assert resolved["pegs"]["dimension_constraint"] == {"step": 32, "offset": 0}
    assert resolved["pegs"]["frame_constraint"] == {"step": 4, "offset": 1}
    # The window peg needs the resolved render window, not the scene duration.
    assert resolved["pegs"]["window_frames"] == resolved["render_end"] - resolved["render_start"]

    # Cache identity must move when a peg source moves, or a pegged render would
    # reuse a set assembled against the previous model.
    before = core.reference_fingerprint(project, 0)
    project.dimension_constraint = {"step": 16, "offset": 0}
    assert core.reference_fingerprint(project, 0) != before
