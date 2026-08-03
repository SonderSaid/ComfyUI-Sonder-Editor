import importlib
import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

from server.timeline_state import (
    REFERENCE_RECIPE_PRESETS,
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
    io = types.SimpleNamespace(
        ComfyNode=_Node,
        Schema=_Schema,
        Custom=lambda _name: _Type,
        Int=_Type,
        Float=_Type,
        String=_Type,
        Image=_Type,
        Audio=_Type,
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
    frames = core.decode_reference_set(core.resolve_reference_set(project, 0))[0]

    # 3 refs on the 8k+1 LTX grid is the 25-frame bucket, split into contiguous
    # segments: the first ref owns index 0 and the last absorbs the +1 tail.
    assert frames.shape[0] == 25
    channel = frames.reshape(25, -1, 3).mean(dim=1).argmax(dim=1).tolist()
    assert channel == [0] * 8 + [1] * 8 + [2] * 9
    # No frame is left black between references.
    assert float(frames.reshape(25, -1).max(dim=1).values.min()) > 0.5


def test_msr_routes_a_context_member_to_the_background_output(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    project = _multi_member_project(
        tmp_path, _preset("sonder:ltx_msr"), [(255, 0, 0), (0, 0, 255)],
        tags=([], ["sonder:location"]),
    )
    result = core.decode_reference_set(core.resolve_reference_set(project, 0))
    frames, context = result[0], result[6]
    # The context member leaves the temporal sequence entirely.
    assert frames.shape[0] == 17
    assert frames.reshape(17, -1, 3).mean(dim=1).argmax(dim=1).unique().tolist() == [0]
    assert float(context[0, :, :, 2].mean()) > 0.5


def test_ingredients_loops_the_sheet_without_constraining_the_render_window(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    project = _multi_member_project(tmp_path, _preset("sonder:ltx_ingredients"), [(255, 0, 0), (0, 255, 0)])
    # The render window is 18 frames — far under the 121-frame reference
    # sequence. That must not refuse the render.
    result = core.decode_reference_set(core.resolve_reference_set(project, 0))
    frames = result[0]
    assert tuple(frames.shape) == (121, 48, 64, 3)
    assert bool((frames[0] == frames[120]).all())
    assert result[6].shape[0] == 1  # context is dead for Ingredients


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


def test_dead_recipe_outputs_emit_documented_fallbacks(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    monkeypatch.setattr(core, "resolve_existing_project_path", lambda project, path, **_kwargs: str(Path(project.project_dir) / path))
    project = _multi_member_project(tmp_path, _preset("sonder:wan_vace"), [(255, 0, 0), (0, 255, 0)])
    result = core.decode_reference_set(core.resolve_reference_set(project, 0))
    # VACE drives reference_frames plus the two strings; everything else is dead.
    assert result[1] == 0                                   # reference_idx
    assert result[2] == 0.0                                 # reference_strength
    assert result[3]["waveform"].abs().max() == 0.0         # silent AUDIO
    assert result[4] and result[5]
    assert float(result[6].abs().max()) == 0.0              # context
    assert all(float(slot.abs().max()) == 0.0 for slot in result[7:])  # r01..r16

    bernini = _multi_member_project(tmp_path / "b", _preset("sonder:wan_bernini"), [(255, 0, 0), (0, 255, 0)])
    slots = core.decode_reference_set(core.resolve_reference_set(bernini, 0))
    assert float(slots[0].abs().max()) == 0.0               # reference_frames is dead
    assert float(slots[7].abs().max()) > 0.0                # r01 carries the member
    assert float(slots[9].abs().max()) == 0.0               # r03 is unstaged


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
    project.generation_queue = [GenerationJob(
        job_id="queued",
        scene_id="scene",
        params={"snapshot_version": 1},
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
    absent = core.decode_reference_set({"has_reference": 0, "width": 64, "height": 48})
    assert len(absent) == 23
    assert tuple(absent[0].shape) == (1, 48, 64, 3)
    assert absent[2] == 0.0
    assert absent[3]["waveform"].shape[1] == 2

    present = core.decode_reference_set(core.resolve_reference_set(project, 0))
    assert len(present) == 23
    assert present[2] == 1.0
    assert present[4] == "image0: red subject"
    assert present[5] == "Hero"
    assert present[7].shape[0] == 1  # r01
    assert tuple(present[8].shape) == (1, 48, 64, 3)  # r02 fallback


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
        core.decode_reference_set(ref)
    project.scenes[0].reference_items[0].members = [{"entity_id": "entity", "member_id": "missing"}]
    with pytest.raises(RuntimeError, match="no longer exists"):
        core.decode_reference_set(core.resolve_reference_set(project, 0))


def test_v3_schema_freezes_selector_and_bridge_socket_names(monkeypatch):
    _install_io(monkeypatch)
    module = _import_module(monkeypatch, "reference_bridge_v3")
    selector = module.SonderReferenceSelector.define_schema()
    bridge = module.SonderReferenceBridge.define_schema()
    assert selector.node_id == "SonderReferenceSelector"
    assert [value.id for value in selector.inputs] == ["project", "reference_lane_index"]
    assert [value.display_name for value in selector.outputs] == ["reference_set", "has_reference"]
    assert bridge.node_id == "SonderReferenceBridge"
    assert [value.display_name for value in bridge.outputs] == [
        "reference_frames", "reference_idx", "reference_strength", "reference_audio",
        "reference_prompt", "reference_names", "context",
        *[f"r{index:02d}" for index in range(1, 17)],
    ]


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
