"""Pixel, persistence and enqueue proofs for project-owned Reference framing."""
import asyncio
import copy
import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from server import media_helpers, project_manager, project_storage, routes
from server.timeline_state import GenerationJob, ReferenceLaneRecipe
from test_reference_bridge_v3 import _import_module, _project
from test_project_mutation_pipeline_backend import DummyRequest, _load_route_module, _route_handler


def patterned_project(tmp_path, assembly="slots", portrait=False, **hard):
    recipe = ReferenceLaneRecipe(recipe={"hard": {
        "assembly": assembly, "output_size": "custom", "width": 16, "height": 16,
        "live_outputs": ["image_slots"], **hard,
    }})
    project = _project(tmp_path, recipe)
    image = np.concatenate([
        np.full((16, 16, 3), color, dtype=np.uint8)
        for color in ((255, 0, 0), (0, 255, 0), (0, 0, 255))
    ], axis=0 if portrait else 1)
    assert cv2.imwrite(str(tmp_path / "media" / "subject.png"), image[:, :, ::-1])
    project.assets[0].height, project.assets[0].width = image.shape[:2]
    return project


@pytest.mark.parametrize("assembly", ["batch", "temporal", "slots"])
@pytest.mark.parametrize("portrait", [False, True])
@pytest.mark.parametrize("mode", media_helpers.FIT_MODES)
def test_each_individual_assembly_emits_the_selected_fitting_pixels(monkeypatch, tmp_path, assembly, portrait, mode):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path, assembly, portrait)
    project.metadata["reference_fit_mode"] = mode
    tensor = core.decode_reference_images(core.resolve_reference_set(project))[0]
    pixels = np.rint(tensor[0].numpy() * 255).astype(np.uint8)
    assert pixels.shape == (16, 16, 3)
    if mode == "cover":
        assert np.all(pixels == [0, 255, 0])
    elif mode == "fit":
        assert np.all(pixels[0, 0] == 0)
        assert np.all(pixels[-1, -1] == 0)
        assert pixels[8, 8, 1] > 240
    else:
        # Edge padding and stretch both retain all three coloured regions.
        if portrait:
            assert np.all(pixels[0, 8] == [255, 0, 0])
            assert np.all(pixels[-1, 8] == [0, 0, 255])
        else:
            assert np.all(pixels[8, 0] == [255, 0, 0])
            assert np.all(pixels[8, -1] == [0, 0, 255])
        assert not np.any(np.all(pixels == 0, axis=-1))
    assert np.all(tensor.numpy() == tensor[0].numpy())


@pytest.mark.parametrize("assembly", ["batch", "temporal", "slots"])
@pytest.mark.parametrize("portrait", [False, True])
def test_fitting_pixels_distinguish_edge_padding_from_stretch(monkeypatch, tmp_path, assembly, portrait):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path, assembly, portrait)
    landscape = np.zeros((16, 48, 3), dtype=np.uint8)
    gradient = np.arange(16, dtype=np.uint8)[:, None] * 16
    for channel in range(3):
        landscape[:, channel * 16:(channel + 1) * 16, channel] = gradient
    source = landscape.transpose(1, 0, 2) if portrait else landscape
    assert cv2.imwrite(str(tmp_path / "media" / "subject.png"), source[:, :, ::-1])
    for mode in media_helpers.FIT_MODES:
        project.metadata["reference_fit_mode"] = mode
        result = core.decode_reference_images(core.resolve_reference_set(project))[0]
        pixels = np.rint(result[0].numpy() * 255).astype(np.uint8)
        if portrait:
            pixels = pixels.transpose(1, 0, 2)
        if mode == "cover":
            assert np.array_equal(pixels[:, 8, 1], np.arange(16) * 16)
            assert not np.any(pixels[:, :, [0, 2]])
        elif mode == "stretch":
            assert np.array_equal(pixels[:, 0, 0], np.arange(16) * 16)
            assert not np.any(pixels[:, 0, 1:])
        else:
            # Contain downsamples 16 rows to 5 with area interpolation. Its
            # first and last averages round to 18 and 222, then bars/padding
            # surround that content. A mistaken stretch cannot pass this.
            assert np.all(pixels[5, 0] == [18, 0, 0])
            assert np.all(pixels[9, 0] == [222, 0, 0])
            assert np.all(pixels[0, 0] == ([0, 0, 0] if mode == "fit" else [18, 0, 0]))
            assert np.all(pixels[-1, 0] == ([0, 0, 0] if mode == "fit" else [222, 0, 0]))


@pytest.mark.parametrize("portrait,anchor,color", [
    (False, "left", [255, 0, 0]), (False, "center", [0, 255, 0]),
    (False, "right", [0, 0, 255]), (True, "top", [255, 0, 0]),
    (True, "center", [0, 255, 0]), (True, "bottom", [0, 0, 255]),
    (False, "top", [0, 255, 0]), (False, "bottom", [0, 255, 0]),
    (True, "left", [0, 255, 0]), (True, "right", [0, 255, 0]),
])
def test_crop_anchor_keeps_the_authored_side(monkeypatch, tmp_path, portrait, anchor, color):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path, portrait=portrait)
    project.metadata["reference_crop_position"] = anchor
    output = core.decode_reference_images(core.resolve_reference_set(project))[0]
    assert np.all(np.rint(output.numpy() * 255) == color)


@pytest.mark.parametrize("mode", media_helpers.FIT_MODES)
def test_library_crop_precedes_recipe_fitting(monkeypatch, tmp_path, mode):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path)
    project.references[0].members[0].crop = {"x": 1 / 3, "y": 0, "w": 1 / 3, "h": 1}
    project.metadata["reference_fit_mode"] = mode
    output = core.decode_reference_images(core.resolve_reference_set(project))[0]
    assert output.shape == (1, 16, 16, 3)
    assert np.all(np.rint(output.numpy() * 255) == [0, 255, 0])


@pytest.mark.parametrize("mode", media_helpers.FIT_MODES)
@pytest.mark.parametrize("geometry,shape", [
    ({"output_size": "scene", "size_multiple": 16}, (48, 64)),
    ({"output_size": "custom", "width": 27, "height": 31, "size_multiple": 8}, (32, 24)),
    ({"output_size": "native", "long_edge_max": 32, "size_multiple": 8}, (8, 32)),
])
def test_fit_choice_preserves_recipe_dimensions(monkeypatch, tmp_path, mode, geometry, shape):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path, **geometry)
    project.metadata["reference_fit_mode"] = mode
    output = core.decode_reference_images(core.resolve_reference_set(project))[0]
    assert tuple(output.shape[1:3]) == shape


@pytest.mark.parametrize("layout", ["grid", "strip"])
def test_sheet_pixels_and_background_ignore_project_fitting(monkeypatch, tmp_path, layout):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path, "sheet", portrait=True, layout=layout, background="white")
    outputs = []
    for mode in media_helpers.FIT_MODES:
        for anchor in media_helpers.CROP_POSITIONS:
            project.metadata.update(reference_fit_mode=mode, reference_crop_position=anchor)
            outputs.append(core.decode_reference_images(core.resolve_reference_set(project))[0].numpy())
    assert all(np.array_equal(outputs[0], output) for output in outputs[1:])
    assert np.all(outputs[0][0, 0, 0] == 1)  # recipe white padding remains


@pytest.mark.parametrize("assembly", ["batch", "slots"])
def test_video_fitting_keeps_frame_count_and_source_order(monkeypatch, tmp_path, assembly):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path, assembly, frame_rate=24, frame_rate_source="custom")
    asset = project.assets[0]
    asset.asset_type, asset.fps, asset.frame_count, asset.duration_sec = "video", 30, 30, 1
    project.references[0].members[0].source_end_sec = 1
    project.metadata.update(reference_fit_mode="cover", reference_crop_position="right")
    monkeypatch.setattr(core, "resolve_source_color_interpretation", lambda *_args, **_kwargs: None)
    calls = []
    def decoded(_path, start, end, **_kwargs):
        calls.append((start, end))
        for index in range(start, end):
            frame = np.zeros((16, 48, 3), dtype=np.uint8)
            frame[:, 32:, 2] = index
            yield frame
    monkeypatch.setattr(core, "decode_video_range", decoded)
    output = core.decode_reference_images(core.resolve_reference_set(project))[0]
    assert output.shape == (24, 16, 16, 3)
    assert calls == [(0, 30)]
    assert np.all(output[0].numpy() == 0)
    assert np.all(np.rint(output[-1, :, :, 2].numpy() * 255) == 29)
    assert np.all(output[:, :, :, :2].numpy() == 0)


def frozen_job(project, params=None):
    scene = project.scenes[0]
    return GenerationJob(
        job_id="frozen", scene_id=scene.scene_id,
        params={"snapshot_version": 1, "prompt_context_format": "prompt_context_v1", **(params or {})},
        reference_lane_count=1,
        reference_lane_configs=[row.to_dict() for row in scene.reference_lane_configs],
        reference_lane_recipes=[row.to_dict() for row in scene.reference_lane_recipes],
        reference_item_snapshots=[row.to_dict() for row in scene.reference_items],
        reference_input_snapshots=[
            *({"kind": "reference", "value": row.to_dict()} for row in project.references),
            *({"kind": "asset", "value": row.to_dict()} for row in project.assets),
        ],
    )


def test_queued_pixels_survive_live_changes_and_component_storage(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path)
    project.metadata.update(reference_fit_mode="cover", reference_crop_position="left")
    job = frozen_job(project)
    routes._freeze_reference_framing_param(project, job)
    project.generation_queue = [job]
    project._execution_context["queue_job_ref_id"] = job.job_id
    first = core.decode_reference_images(core.resolve_reference_set(project))[0].numpy()
    project.metadata.update(reference_fit_mode="stretch", reference_crop_position="right")
    project_manager.save_project(project, notify=False)
    restored = project_manager.load_project(project.project_dir)
    restored._execution_context = dict(project._execution_context)
    restored_job = project_storage.resolve_queue_job(restored, job.job_id)
    assert restored_job.params["reference_fit_mode"] == "cover"
    assert restored_job.params["reference_crop_position"] == "left"
    second = core.decode_reference_images(core.resolve_reference_set(restored))[0].numpy()
    assert np.array_equal(first, second)
    assert np.all(np.rint(second * 255) == [255, 0, 0])
    # Reusing an explicit frozen envelope does not pick up live settings.
    routes._freeze_reference_framing_param(restored, restored_job)
    assert restored_job.params["reference_fit_mode"] == "cover"


def test_historical_snapshot_retains_edge_padding_while_live_defaults_to_crop(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path)
    job = frozen_job(project)
    project.generation_queue = [job]
    project._execution_context["queue_job_ref_id"] = job.job_id
    frozen = core.resolve_reference_set(project)
    assert frozen["framing"] == {"fit_mode": "pad_edge", "crop_position": "center"}
    output = core.decode_reference_images(frozen)[0]
    assert np.all(np.rint(output[0, 8, 0].numpy() * 255) == [255, 0, 0])
    project._execution_context.pop("queue_job_ref_id")
    live = core.resolve_reference_set(project)
    assert live["framing"]["fit_mode"] == "cover"
    assert np.all(np.rint(core.decode_reference_images(live)[0].numpy() * 255) == [0, 255, 0])
    assert "reference_fit_mode" not in job.params
    assert "reference_fit_mode" not in project.metadata


def test_a_queued_job_without_a_snapshot_follows_the_live_framing(monkeypatch, tmp_path):
    # Such a job (a marker-absent v0.2.2 request) renders live References, so
    # its framing is live too; edge padding is only a frozen snapshot's fallback.
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path)
    project.metadata.update(reference_fit_mode="stretch", reference_crop_position="right")
    scene = project.scenes[0]
    job = GenerationJob(job_id="live", scene_id=scene.scene_id, params={})
    project.generation_queue = [job]
    project._execution_context["queue_job_ref_id"] = job.job_id
    resolved = core.resolve_reference_set(project)
    assert resolved["framing"] == {"fit_mode": "stretch", "crop_position": "right"}
    project._execution_context.pop("queue_job_ref_id")
    assert np.array_equal(core.decode_reference_images(resolved)[0].numpy(),
                          core.decode_reference_images(core.resolve_reference_set(project))[0].numpy())
    assert job.params == {}


def test_fingerprint_tracks_live_framing_even_without_a_revision_change(monkeypatch, tmp_path):
    core = _import_module(monkeypatch, "reference_core")
    project = patterned_project(tmp_path)
    first = core.reference_fingerprint(project)
    project.metadata["reference_fit_mode"] = "stretch"
    second = core.reference_fingerprint(project)
    project.metadata["reference_crop_position"] = "right"
    third = core.reference_fingerprint(project)
    assert len({first, second, third}) == 3
    job = frozen_job(project, {"reference_fit_mode": "fit", "reference_crop_position": "left"})
    project.generation_queue = [job]
    project._execution_context["queue_job_ref_id"] = job.job_id
    frozen = core.reference_fingerprint(project)
    project.metadata["reference_fit_mode"] = "cover"
    assert core.reference_fingerprint(project) == frozen


@pytest.mark.parametrize("values", [
    None, [], "wrong", 7, {},
    {"reference_fit_mode": {}, "reference_crop_position": []},
    {"reference_fit_mode": "unknown", "reference_crop_position": None},
])
def test_stored_malformed_framing_is_tolerated_without_rewriting(values):
    original = copy.deepcopy(values)
    assert media_helpers.resolve_reference_framing(values) == {"fit_mode": "cover", "crop_position": "center"}
    assert media_helpers.resolve_reference_framing(values, snapshot=True) == {"fit_mode": "pad_edge", "crop_position": "center"}
    assert values == original


@pytest.mark.parametrize("field,value", [
    ("reference_fit_mode", "bad"), ("reference_fit_mode", {}), ("reference_crop_position", "bad"),
])
def test_invalid_project_write_refuses_before_any_other_change(monkeypatch, tmp_path, field, value):
    module = _load_route_module(monkeypatch)
    project = patterned_project(tmp_path)
    saves = []
    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "save_project", lambda *args, **kwargs: saves.append(args))
    before = project.to_dict()
    response = asyncio.run(_route_handler(module, "PUT", "/sonder-editor/project/{project_id}")(
        DummyRequest(method="PUT", match_info={"project_id": "project"},
                     body={"name": "must not change", "metadata": {field: value}})))
    assert response.status == 400
    assert project.to_dict() == before
    assert not saves


def test_valid_project_write_patches_only_the_authored_field_and_persists(monkeypatch, tmp_path):
    module = _load_route_module(monkeypatch)
    project = patterned_project(tmp_path)
    project.metadata.update(reference_fit_mode="cover", reference_crop_position="right", unrelated={"kept": True})
    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    response = asyncio.run(_route_handler(module, "PUT", "/sonder-editor/project/{project_id}")(
        DummyRequest(method="PUT", match_info={"project_id": "project"},
                     body={"metadata": {"reference_fit_mode": "stretch"}})))
    assert response.status == 200
    restored = project_manager.load_project(project.project_dir)
    assert restored.metadata["reference_fit_mode"] == "stretch"
    assert restored.metadata["reference_crop_position"] == "right"
    assert restored.metadata["unrelated"] == {"kept": True}


@pytest.mark.parametrize("batch", [False, True])
def test_single_and_batch_routes_freeze_settings_and_preserve_explicit_params(monkeypatch, tmp_path, batch):
    module = _load_route_module(monkeypatch)
    project = patterned_project(tmp_path)
    project.metadata.update(reference_fit_mode="fit", reference_crop_position="bottom")
    monkeypatch.setattr(module, "_apply_queue_versioned_sync",
                        lambda request, apply: (project, apply(project)[1]))
    body = {"scene_id": "scene", "selection_start": 12, "selection_end": 30,
            "scene_width": 16, "scene_height": 16,
            "params": {"prompt_context_format": "prompt_context_v1"}}
    if batch:
        explicit = copy.deepcopy(body)
        explicit["params"].update(reference_fit_mode="cover", reference_crop_position="left")
        request_body, path = {"jobs": [body, explicit]}, "/sonder-editor/project/{project_id}/queue/batch"
    else:
        request_body, path = body, "/sonder-editor/project/{project_id}/queue"
    response = asyncio.run(_route_handler(module, "POST", path)(
        DummyRequest(match_info={"project_id": "project"}, body=request_body)))
    assert response.status == (201 if batch else 200)
    assert project.generation_queue[0].params["reference_fit_mode"] == "fit"
    assert project.generation_queue[0].params["reference_crop_position"] == "bottom"
    if batch:
        assert project.generation_queue[1].params["reference_fit_mode"] == "cover"
        assert project.generation_queue[1].params["reference_crop_position"] == "left"


def test_released_v022_envelope_does_not_gain_unknown_reference_fields():
    from server import frozen_prompt
    from server.timeline_state import TimelineProject
    job = GenerationJob(params={"snapshot_version": 1})
    routes._freeze_reference_framing_param(TimelineProject(), job)
    assert frozen_prompt.classify_frozen_prompt(job) == "v0.2.2"
