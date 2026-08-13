"""Reference selector and bridge execution core.

The selector side is intentionally metadata-only.  Media is resolved and
decoded only by :mod:`reference_bridge_v3` when the lazy reference branch is
actually evaluated.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from typing import Any

import cv2
import numpy as np
import torch

from ..server.media_helpers import (
    apply_rgb_color_correction,
    color_correction_for_interpretation,
    decode_audio_samples,
    decode_video_frame,
    decode_video_range,
    fit_frame_to_canvas,
    resolve_source_color_interpretation,
)
from ..server.path_security import resolve_existing_project_path
from ..server.frozen_reference import decode_frozen_reference_catalog
from ..server.reference_prompt_formatter import (
    PROJECT_SCOPED_PROMPT_TOKENS,
    build_reference_formatter_context,
    format_reference_prompt,
    member_prompt_fragment as _shared_member_prompt_fragment,
    reference_member_labels,
)
from ..server.reference_resolution import reference_live_outputs, resolve_effective_references
from ..server.timeline_state import (
    Asset,
    LaneConfig,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    effective_scene_fps,
)


MAX_REFERENCE_SLOTS = 16


def _int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _float(value, default=0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _snapshot_version(job) -> int:
    params = getattr(job, "params", {}) or {}
    return max(0, _int(params.get("snapshot_version", 0), 0)) if isinstance(params, dict) else 0


def _find_queue_job(project):
    context = getattr(project, "_execution_context", None) or {}
    job_id = str(context.get("queue_job_ref_id", "") or "")
    if not job_id:
        return None
    return next(
        (job for job in (getattr(project, "generation_queue", []) or []) if getattr(job, "job_id", "") == job_id),
        None,
    )


def _snapshot_catalog_project(project, job):
    """Return a shallow project view backed by the job's frozen References."""
    references, assets = decode_frozen_reference_catalog(job)
    frozen = copy.copy(project)
    frozen.references = references
    frozen.assets = assets
    return frozen


def _active_scene(project):
    context = getattr(project, "_execution_context", None) or {}
    scene_id = str(context.get("scene_id", "") or "")
    if scene_id:
        scene = project.get_scene(scene_id)
        if scene is not None:
            return scene
    scenes = getattr(project, "scenes", []) or []
    return scenes[0] if scenes else None


def _render_window(project, scene) -> tuple[int, int, int, int]:
    context = getattr(project, "_execution_context", None) or {}
    start = _int(context.get("context_start"), 0)
    default_end = _int(getattr(scene, "duration_frames", 0), 0) if scene is not None else 0
    end = _int(context.get("context_end"), default_end)
    project_width, project_height = getattr(project, "resolution", (1280, 720))
    width = _int(getattr(scene, "width", 0), 0) or _int(project_width, 1280)
    height = _int(getattr(scene, "height", 0), 0) or _int(project_height, 720)
    return max(0, start), max(start + 1, end), max(1, width), max(1, height)


def _source(project, scene) -> dict[str, Any]:
    job = _find_queue_job(project)
    if job is not None and _snapshot_version(job) > 0:
        catalog_project = _snapshot_catalog_project(project, job)
        lane_count = max(1, _int(getattr(job, "reference_lane_count", 1), 1))
        configs = [
            LaneConfig.from_dict(value) if isinstance(value, dict) else value
            for value in (getattr(job, "reference_lane_configs", []) or [])
        ]
        recipes = [
            ReferenceLaneRecipe.from_dict(value) if isinstance(value, dict) else value
            for value in (getattr(job, "reference_lane_recipes", []) or [])
        ]
        items = [
            ReferenceItem.from_dict(value)
            for value in (getattr(job, "reference_item_snapshots", []) or [])
            if isinstance(value, dict)
        ]
        # Frozen at enqueue so a queued job resolves the same set it was
        # queued with, mirroring how the prompt threshold rides job params.
        threshold = _float((getattr(job, "params", {}) or {}).get("reference_frame_threshold", 0.0))
        frozen_scene_fps = _float(getattr(job, "scene_fps", 0.0), 0.0)
        pegs = {
            "frame_constraint": getattr(job, "frame_constraint", None),
            "dimension_constraint": getattr(job, "dimension_constraint", None),
            "fps": frozen_scene_fps if frozen_scene_fps > 0 else effective_scene_fps(project, None),
        }
        source_name = "snapshot"
    else:
        catalog_project = project
        lane_count = max(1, _int(getattr(scene, "reference_lane_count", 1), 1)) if scene else 1
        configs = list(getattr(scene, "reference_lane_configs", []) or []) if scene else []
        recipes = list(getattr(scene, "reference_lane_recipes", []) or []) if scene else []
        items = list(getattr(scene, "reference_items", []) or []) if scene else []
        threshold = _float((getattr(project, "metadata", {}) or {}).get("reference_frame_threshold", 0.0))
        pegs = {
            "frame_constraint": getattr(project, "frame_constraint", None),
            "dimension_constraint": getattr(project, "dimension_constraint", None),
            "fps": effective_scene_fps(project, scene),
        }
        source_name = "live"
    while len(configs) < lane_count:
        configs.append(LaneConfig())
    while len(recipes) < lane_count:
        recipes.append(ReferenceLaneRecipe())
    return {
        "source": source_name,
        "lane_count": lane_count,
        "configs": configs[:lane_count],
        "recipes": recipes[:lane_count],
        "items": items,
        "frame_threshold_pct": threshold,
        "pegs": pegs,
        "catalog_project": catalog_project,
    }


_VOICE_TAGS = ("sonder:voice_identity",)


def build_reference_registry(project, resolved_lanes) -> dict[str, Any]:
    """Project-scoped, cross-lane numbering for the four reference labels.

    Four independent dense sequences, because MiniMax's own example pairs
    `<Subject 3>` with `(S1)` — speakers are a different population, not a
    renaming of subjects.
    """
    context = build_reference_formatter_context(
        winners=resolved_lanes,
        catalog_records=getattr(project, "references", None) or [],
        assets=getattr(project, "assets", None) or [],
        recipes=[],
        setup_data={},
    )
    return context["registry"]


def registry_numbers_for(registry, reference, member) -> dict:
    """The four project-scoped token values for one staged member."""
    registry = registry if isinstance(registry, dict) else {}
    entity_id = str(getattr(reference, "reference_id", "") or "")
    member_id = str(getattr(member, "member_id", "") or "")
    return {
        "subject_n": (registry.get("subjects") or {}).get(entity_id, 0),
        "picture_n": (registry.get("pictures") or {}).get(member_id, 0),
        "audio_n": (registry.get("audios") or {}).get(member_id, 0),
        "speaker_n": (registry.get("speakers") or {}).get(entity_id, 0),
    }


def resolve_reference_set(project, reference_lane_index=0) -> dict[str, Any]:
    """Resolve one lane against the current execution window without decoding."""

    lane_index = max(0, _int(reference_lane_index, 0))
    scene = _active_scene(project)
    render_start, render_end, width, height = _render_window(project, scene)
    source = _source(project, scene)
    explicit_item_ends = [
        _int(getattr(item, "end_frame", -1), -1)
        for item in source["items"]
    ]
    duration = max(
        render_end,
        _int(getattr(scene, "duration_frames", 0), render_end),
        *(end for end in explicit_item_ends if end >= 0),
    )
    selected = None
    registry = {"subjects": {}, "pictures": {}, "audios": {}, "speakers": {}}
    if lane_index < source["lane_count"]:
        resolved = resolve_effective_references(
            reference_items=source["items"],
            window_start=render_start,
            window_end=render_end,
            scene_duration=duration,
            lane_configs=source["configs"],
            lane_count=source["lane_count"],
            frame_threshold_pct=source["frame_threshold_pct"],
        )
        lane_value = resolved[lane_index] if lane_index < len(resolved) else None
        selected = lane_value.get("item") if isinstance(lane_value, dict) else None
        # Over ALL lanes, not just this one: resolve_effective_references
        # already returns every lane, and the registry must be cross-lane so an
        # image lane and an audio lane staging the same entity agree.
        registry = build_reference_registry(source["catalog_project"], resolved)
    recipe = source["recipes"][lane_index] if lane_index < len(source["recipes"]) else ReferenceLaneRecipe()
    item_dict = selected.to_dict() if hasattr(selected, "to_dict") else dict(selected or {})
    recipe_dict = recipe.to_dict() if hasattr(recipe, "to_dict") else dict(recipe or {})
    return {
        "project": source["catalog_project"],
        "source": source["source"],
        "lane_index": lane_index,
        "has_reference": int(bool(item_dict)),
        "strength": _float(item_dict.get("strength"), 1.0) if item_dict else 0.0,
        "item": item_dict,
        "recipe": recipe_dict,
        "render_start": render_start,
        "render_end": render_end,
        "width": width,
        "height": height,
        # Peg sources travel with the resolved set so the bridge resolves them
        # the same way whether it runs live or from a frozen job, and so they
        # land in the selector fingerprint.
        "pegs": {**source["pegs"], "window_frames": max(0, render_end - render_start)},
        # Rides reference_fingerprint (which hashes the whole dict) and inherits
        # the snapshot-vs-live split in _source, so it reaches _assemble_prompt
        # and _member_prompts without a second resolution pass.
        "registry": registry,
    }


def reference_fingerprint(project, reference_lane_index=0):
    """Return deterministic cache identity, or NaN when V3 hides the context."""

    if project is None:
        return float("nan")
    try:
        ref = resolve_reference_set(project, reference_lane_index)
        stable = {key: value for key, value in ref.items() if key != "project"}
        # The item snapshot intentionally keeps member ids, while the Library
        # metadata and media records remain live until execution. Include the
        # project revision so those dependencies cannot reuse a stale selector.
        stable["project_id"] = str(getattr(project, "project_id", "") or "")
        stable["project_modified_at"] = str(getattr(project, "modified_at", "") or "")
        payload = json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
    except Exception:
        return float("nan")


def _empty_image(width: int, height: int) -> torch.Tensor:
    return torch.zeros((1, max(1, height), max(1, width), 3), dtype=torch.float32)


def _silent_audio(duration_sec=1.0, sample_rate=44100) -> dict:
    samples = max(1, int(max(0.0, duration_sec) * sample_rate))
    return {"waveform": torch.zeros((1, 2, samples), dtype=torch.float32), "sample_rate": sample_rate}


def _member_records(project, item: dict) -> list[dict[str, Any]]:
    references = getattr(project, "references", []) or []
    assets = {str(asset.asset_id): asset for asset in (getattr(project, "assets", []) or [])}
    lookup = {
        str(member.member_id): (reference, member)
        for reference in references
        for member in (getattr(reference, "members", []) or [])
    }
    records = []
    for member_ref in item.get("members", []) or []:
        member_id = str(member_ref.get("member_id", "") or "") if isinstance(member_ref, dict) else ""
        resolved = lookup.get(member_id)
        if resolved is None:
            raise RuntimeError(f"Reference item is present but member {member_id or '<blank>'} no longer exists.")
        reference, member = resolved
        asset = assets.get(str(getattr(member, "asset_id", "") or ""))
        if asset is None:
            raise RuntimeError(f"Reference member {member_id} is present but its asset no longer exists.")
        path = resolve_existing_project_path(project, getattr(asset, "path", ""), purpose="reference member media")
        if not path or not os.path.isfile(path):
            raise RuntimeError(f"Reference member {member_id} is present but its media is unreadable.")
        records.append({"reference": reference, "member": member, "asset": asset, "path": path})
    if not records:
        raise RuntimeError("Reference set is marked present but contains no member metadata.")
    return records


def _apply_member_crop(frame: np.ndarray, crop) -> np.ndarray:
    if not isinstance(crop, dict):
        return frame
    height, width = frame.shape[:2]
    x0 = max(0, min(width - 1, round(_float(crop.get("x")) * width)))
    y0 = max(0, min(height - 1, round(_float(crop.get("y")) * height)))
    x1 = max(x0 + 1, min(width, round((_float(crop.get("x")) + _float(crop.get("w"), 1.0)) * width)))
    y1 = max(y0 + 1, min(height, round((_float(crop.get("y")) + _float(crop.get("h"), 1.0)) * height)))
    return frame[y0:y1, x0:x1]


def _asset_video_fps(asset) -> float:
    value = _float(getattr(asset, "fps", 0.0), 0.0)
    return value if value > 0 else 24.0


def _snap_span_frame_count(target_count: int, hard: dict | None = None) -> int:
    """Apply a recipe's temporal grid without ever extending the source span."""
    hard = hard if isinstance(hard, dict) else {}
    target = max(1, _int(target_count, 1))
    if str(hard.get("frame_count_snap") or "nearest") != "floor_grid":
        return target
    step = max(1, _int(hard.get("frame_step"), 1))
    offset = max(0, _int(hard.get("frame_offset"), 0))
    minimum = max(1, _int(hard.get("minimum_frames"), 1))
    span = max(minimum, target)
    return max(minimum, offset + max(0, (span - offset) // step) * step)


def _load_member_image(record: dict[str, Any]) -> np.ndarray:
    asset, member, path = record["asset"], record["member"], record["path"]
    if getattr(asset, "asset_type", "") == "video":
        fps = _asset_video_fps(asset)
        frame_index = max(0, round(_float(getattr(member, "source_start_sec", 0.0)) * fps))
        interpretation = resolve_source_color_interpretation(asset, path)
        frame = decode_video_frame(path, frame_index, color_interpretation=interpretation)
    else:
        raw = cv2.imread(path, cv2.IMREAD_COLOR)
        frame = cv2.cvtColor(raw, cv2.COLOR_BGR2RGB) if raw is not None else None
        if frame is not None:
            interpretation = resolve_source_color_interpretation(asset, path, allow_probe=False)
            frame = apply_rgb_color_correction(frame, color_correction_for_interpretation(interpretation))
    if frame is None or frame.size == 0:
        raise RuntimeError(f"Reference member {getattr(member, 'member_id', '')} could not be decoded.")
    return _apply_member_crop(frame, getattr(member, "crop", None))


def _load_member_images(
    record: dict[str, Any],
    *,
    decode_span: bool,
    target_fps: float,
    hard: dict | None = None,
) -> list[np.ndarray]:
    """Decode one still or a resampled video span without buffering raw ffmpeg output."""
    asset, member = record["asset"], record["member"]
    if getattr(asset, "asset_type", "") != "video" or not decode_span:
        return [_load_member_image(record)]

    source_fps = _asset_video_fps(asset)
    start_sec = max(0.0, _float(getattr(member, "source_start_sec", 0.0), 0.0))
    raw_end = getattr(member, "source_end_sec", None)
    if raw_end is None:
        end_sec = _float(getattr(asset, "duration_sec", 0.0), 0.0)
        if end_sec <= start_sec:
            native_count = max(0, _int(getattr(asset, "frame_count", 0), 0))
            end_sec = native_count / source_fps if native_count > 0 else start_sec + (1.0 / source_fps)
    else:
        end_sec = _float(raw_end, start_sec)
    end_sec = max(start_sec + (1.0 / source_fps), end_sec)

    source_start = max(0, int(math.floor(start_sec * source_fps + 0.5)))
    source_end = max(source_start + 1, int(math.ceil(end_sec * source_fps)))
    native_count = max(0, _int(getattr(asset, "frame_count", 0), 0))
    if native_count > 0:
        source_end = min(native_count, source_end)
    source_end = max(source_start + 1, source_end)

    served_fps = max(0.001, _float(target_fps, source_fps))
    source_count = source_end - source_start
    target_count = max(1, int(math.floor((source_count / source_fps) * served_fps + 0.5)))
    target_count = _snap_span_frame_count(target_count, hard)
    rate_ratio = source_fps / served_fps
    requested = [
        min(source_count - 1, max(0, int(math.floor((index + 0.5) * rate_ratio))))
        for index in range(target_count)
    ]
    by_source_index: dict[int, int] = {}
    for relative_index in requested:
        by_source_index[relative_index] = by_source_index.get(relative_index, 0) + 1

    interpretation = resolve_source_color_interpretation(asset, record["path"])
    frames = []
    decode_end = source_start + max(requested) + 1
    for relative_index, frame in enumerate(decode_video_range(
        record["path"],
        source_start,
        decode_end,
        color_interpretation=interpretation,
    )):
        repeats = by_source_index.get(relative_index, 0)
        if not repeats:
            continue
        cropped = _apply_member_crop(frame, getattr(member, "crop", None))
        frames.extend(cropped.copy() for _ in range(repeats))
    if len(frames) != target_count:
        raise RuntimeError(
            f"Reference video member {getattr(member, 'member_id', '')} decoded "
            f"{len(frames)} of {target_count} requested frames."
        )
    return frames


def resolve_pegged_hard(hard: dict, pegs: dict) -> dict:
    """Resolve `*_source` pegs against live constraints, once per assembly.

    A pegged value tracks whatever the scene is actually rendering with instead
    of the number that was authored. `pegs` supplies the resolved sources; a peg
    whose source is missing keeps the authored value, so a project with no
    resolved constraint degrades to what the user typed rather than to zero.
    """
    if not isinstance(hard, dict):
        return {}
    resolved = dict(hard)
    frame_constraint = pegs.get("frame_constraint") if isinstance(pegs.get("frame_constraint"), dict) else None
    if str(resolved.get("frame_grid_source", "custom")) == "template" and frame_constraint:
        step = _int(frame_constraint.get("step"), 0)
        if step > 0:
            resolved["frame_step"] = step
            resolved["frame_offset"] = max(0, _int(frame_constraint.get("offset"), 0))
    dimension_constraint = pegs.get("dimension_constraint") if isinstance(pegs.get("dimension_constraint"), dict) else None
    if str(resolved.get("size_multiple_source", "custom")) == "template" and dimension_constraint:
        step = _int(dimension_constraint.get("step"), 0)
        if step > 0:
            resolved["size_multiple"] = step
    if str(resolved.get("loop_frames_source", "custom")) == "custom":
        return resolved
    window = max(0, _int(pegs.get("window_frames"), 0))
    if window > 0:
        resolved["loop_frames"] = window
    return resolved


def _snap_dimension(value: int, multiple: int, *, ceiling=None, floor=False) -> int:
    multiple = max(1, int(multiple))
    value = max(multiple, int(value))
    raw = (value // multiple) * multiple if floor else round(value / multiple) * multiple
    result = max(multiple, raw)
    if ceiling:
        result = min(result, max(multiple, (int(ceiling) // multiple) * multiple))
    return result


def _member_geometry(
    frame: np.ndarray,
    hard: dict,
    output_width: int,
    output_height: int,
    member_count: int = 1,
) -> tuple[int, int]:
    def bounded(width_value, height_value, *, floor=False, ceiling=None):
        width_value = max(1.0, float(width_value))
        height_value = max(1.0, float(height_value))
        short_edge_max = max(0, _int(hard.get("short_edge_max"), 0))
        if short_edge_max and min(width_value, height_value) > short_edge_max:
            scale = short_edge_max / min(width_value, height_value)
            width_value *= scale
            height_value *= scale
            floor = True
        floor = floor or str(hard.get("size_rounding") or "nearest") == "floor"
        return (
            _snap_dimension(width_value, multiple, floor=floor, ceiling=ceiling),
            _snap_dimension(height_value, multiple, floor=floor, ceiling=ceiling),
        )

    mode = str(hard.get("output_size", "scene") or "scene")
    multiple = max(1, _int(hard.get("size_multiple"), 1))
    if mode == "native":
        height, width = frame.shape[:2]
        # A short-edge contract and the generic long-edge default are mutually
        # exclusive. Only apply the 848 fallback when no explicit short-edge
        # model contract exists.
        if max(0, _int(hard.get("short_edge_max"), 0)) <= 0:
            long_edge = min(max(width, height), max(16, _int(hard.get("long_edge_max"), 848)))
            scale = long_edge / max(1, max(width, height))
            return bounded(width * scale, height * scale, ceiling=long_edge)
        return bounded(width, height)
    # A lone member may have its own canonical size (Best Face ID's bust crop is
    # ~460x406 where its 4-panel sheet is exactly 1536x1024).
    single = hard.get("single_member_size")
    if member_count <= 1 and isinstance(single, list) and len(single) >= 2:
        return bounded(_int(single[0], output_width), _int(single[1], output_height))
    if mode == "custom" and _int(hard.get("width"), 0) and _int(hard.get("height"), 0):
        return bounded(_int(hard["width"]), _int(hard["height"]))
    if multiple > 1:
        # Floor, never round up: exceeding the requested output is worse than
        # losing a few pixels (this is what VACE's /16 rule needs).
        return bounded(output_width, output_height, floor=True)
    return bounded(output_width, output_height)


def _to_tensor(frame: np.ndarray, width: int, height: int) -> torch.Tensor:
    fitted, _ = fit_frame_to_canvas(frame, width, height, mode="pad_edge", crop_position="center")
    return torch.from_numpy(np.ascontiguousarray(fitted, dtype=np.float32) / 255.0)


def _sheet(images: list[np.ndarray], width: int, height: int, background: str) -> torch.Tensor:
    count = max(1, len(images))
    columns = max(1, min(count, math.ceil(math.sqrt(count * width / max(1, height)))))
    rows = max(1, math.ceil(count / columns))
    value = 255 if background == "white" else 0
    canvas = np.full((height, width, 3), value, dtype=np.uint8)
    for index, image in enumerate(images):
        row, column = divmod(index, columns)
        x0, x1 = round(column * width / columns), round((column + 1) * width / columns)
        y0, y1 = round(row * height / rows), round((row + 1) * height / rows)
        tile_width, tile_height = max(1, x1 - x0), max(1, y1 - y0)
        image_height, image_width = image.shape[:2]
        scale = min(tile_width / max(1, image_width), tile_height / max(1, image_height))
        fitted_width = max(1, min(tile_width, round(image_width * scale)))
        fitted_height = max(1, min(tile_height, round(image_height * scale)))
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        fitted = cv2.resize(image, (fitted_width, fitted_height), interpolation=interpolation)
        paste_x = x0 + (tile_width - fitted_width) // 2
        paste_y = y0 + (tile_height - fitted_height) // 2
        canvas[paste_y:paste_y + fitted_height, paste_x:paste_x + fitted_width] = fitted
    return torch.from_numpy(np.ascontiguousarray(canvas, dtype=np.float32) / 255.0).unsqueeze(0)


def _strip(images: list[np.ndarray], width: int, height: int, background: str) -> torch.Tensor:
    """Equal-width vertical strip, padded to the output aspect.

    Mirrors the kijai VACE wrapper, which is the reference implementation for
    the sheet native VACE needs: members are scaled to one common width and
    concatenated, then the strip is padded to the output aspect rather than
    stretched.
    """
    value = 255 if background == "white" else 0
    # Widest member sets the common width, so no member is resampled below its
    # own resolution before the single fit into the output canvas.
    common_width = max(1, max(image.shape[1] for image in images))
    scaled = []
    for image in images:
        image_height, image_width = image.shape[:2]
        scale = common_width / max(1, image_width)
        interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
        scaled.append(cv2.resize(image, (common_width, max(1, round(image_height * scale))), interpolation=interpolation))
    strip = np.concatenate(scaled, axis=0)
    strip_height, strip_width = strip.shape[:2]
    scale = min(width / strip_width, height / strip_height)
    fitted_width = max(1, min(width, round(strip_width * scale)))
    fitted_height = max(1, min(height, round(strip_height * scale)))
    interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
    fitted = cv2.resize(strip, (fitted_width, fitted_height), interpolation=interpolation)
    canvas = np.full((height, width, 3), value, dtype=np.uint8)
    paste_x, paste_y = (width - fitted_width) // 2, (height - fitted_height) // 2
    canvas[paste_y:paste_y + fitted_height, paste_x:paste_x + fitted_width] = fitted
    return torch.from_numpy(np.ascontiguousarray(canvas, dtype=np.float32) / 255.0).unsqueeze(0)


def _grid_frame_count(hard: dict, minimum: int) -> int:
    """Snap ``minimum`` up onto the recipe's ``step * k + offset`` frame grid."""
    step = max(1, _int(hard.get("frame_step"), 8))
    offset = max(0, _int(hard.get("frame_offset"), 1))
    span = max(minimum, step + offset)
    remainder = (span - offset) % step
    return span if remainder == 0 else span + (step - remainder)


def member_prompt_fragment(pattern: str, index: int, prompt: str, name: str,
                           registry_numbers: dict | None = None,
                           member_name: str = "") -> str:
    """Expand one member's slice of the derived prompt.

    A pattern may use `{n}` (1-based), `{index}` (0-based), `{prompt}` and
    `{name}` as many times as it likes, so a recipe can compose a sentence like
    `<Subject {n}> is {prompt}, from <Picture {n}>` rather than only prefixing a
    token. With no `{prompt}`/`{name}` placeholder the member text is appended
    after the expanded pattern, which is what the original `token: label` form
    did and is why existing recipes keep composing identically.

    `{n}` stays LANE-LOCAL — the member's order within this item. The four
    project-scoped tokens (`{subject_n}`, `{picture_n}`, `{audio_n}`,
    `{speaker_n}`) come from the cross-lane registry instead, so the same
    entity gets the same number on an image lane and an audio lane. They are
    collision-free against `{n}` under both the Python `.replace` chain and the
    JS `.split/.join` chain because each is a distinct whole token.
    """
    return _shared_member_prompt_fragment(
        pattern, index, prompt, name, registry_numbers, member_name)


def _formatted_prompts(item: dict, records: list[dict[str, Any]], recipe: dict,
                       registry: dict | None = None) -> tuple[str, list[str]]:
    members = [{
        "prompt": getattr(record["member"], "prompt", ""),
        "entity_name": getattr(record["reference"], "name", ""),
        "member_name": getattr(record["member"], "name", ""),
        "registry_numbers": registry_numbers_for(
            registry, record["reference"], record["member"]),
    } for record in records]
    return format_reference_prompt(item=item, members=members, recipe=recipe)


def _assemble_prompt(item: dict, records: list[dict[str, Any]], recipe: dict,
                     registry: dict | None = None) -> str:
    return _formatted_prompts(item, records, recipe, registry)[0]


def _member_prompts(records: list[dict[str, Any]], recipe: dict,
                    registry: dict | None = None) -> list[str]:
    """Per-slot prompt text, one entry per staged member, for p01..p16."""
    return _formatted_prompts({}, records, recipe, registry)[1]


def _audio_output(record: dict[str, Any]) -> dict:
    member = record["member"]
    samples, sample_rate = decode_audio_samples(record["path"], sample_rate=44100, channels=2, mix_to_mono=False)
    start = max(0, round(_float(getattr(member, "source_start_sec", 0.0)) * sample_rate))
    raw_end = getattr(member, "source_end_sec", None)
    end = samples.shape[1] if raw_end is None else min(samples.shape[1], max(start + 1, round(_float(raw_end) * sample_rate)))
    waveform = np.ascontiguousarray(samples[:, start:end], dtype=np.float32)
    if waveform.size == 0:
        raise RuntimeError(f"Reference audio member {getattr(member, 'member_id', '')} decoded no samples.")
    return {"waveform": torch.from_numpy(waveform).unsqueeze(0), "sample_rate": int(sample_rate)}


def _reference_decode_context(reference_set, expected_media_kind: str | None = None) -> dict[str, Any]:
    ref = reference_set if isinstance(reference_set, dict) else {}
    width = max(1, _int(ref.get("width"), 1280))
    height = max(1, _int(ref.get("height"), 720))
    if not ref.get("has_reference"):
        return {"present": False, "ref": ref, "width": width, "height": height}

    recipe_wrapper = ref.get("recipe") if isinstance(ref.get("recipe"), dict) else {}
    media_kind = "audio" if recipe_wrapper.get("media_kind") == "audio" else "image"
    if expected_media_kind and media_kind != expected_media_kind:
        bridge_name = "Image" if expected_media_kind == "image" else "Audio"
        article = "an" if media_kind[:1].lower() in "aeiou" else "a"
        raise RuntimeError(
            f"Sonder Reference {bridge_name} Bridge cannot decode {article} {media_kind} Reference lane. "
            f"Connect this Selector to the {media_kind.title()} Bridge instead."
        )

    recipe = recipe_wrapper.get("recipe") if isinstance(recipe_wrapper.get("recipe"), dict) else {}
    hard = resolve_pegged_hard(
        recipe.get("hard") if isinstance(recipe.get("hard"), dict) else {},
        ref.get("pegs") if isinstance(ref.get("pegs"), dict) else {},
    )
    item = ref.get("item") if isinstance(ref.get("item"), dict) else {}
    records = _member_records(ref.get("project"), item)
    cap = min(MAX_REFERENCE_SLOTS, max(1, _int(hard.get("max_members"), MAX_REFERENCE_SLOTS)))
    if len(records) > cap:
        raise RuntimeError(f"Reference recipe accepts at most {cap} members; {len(records)} were staged.")

    return {
        "present": True,
        "ref": ref,
        "width": width,
        "height": height,
        "item": item,
        "recipe": recipe,
        "hard": hard,
        "live": reference_live_outputs(hard),
        "records": records,
        "media_kind": media_kind,
    }


def _reference_frame_rate(context: dict[str, Any]) -> float:
    hard = context["hard"]
    source = str(hard.get("frame_rate_source", "scene") or "scene")
    pegs = context["ref"].get("pegs") if isinstance(context["ref"].get("pegs"), dict) else {}
    scene_fps = max(0.001, _float(pegs.get("fps"), 24.0))
    if source == "custom":
        return max(0.001, _float(hard.get("frame_rate"), scene_fps))
    if source == "native":
        # Several video members feed one sequence. The first staged video's rate
        # is authoritative; later videos resample to it. With no video, fall
        # back to the effective scene rate.
        first_video = next((
            record for record in context["records"]
            if getattr(record["asset"], "asset_type", "") == "video"
        ), None)
        if first_video is not None:
            return _asset_video_fps(first_video["asset"])
    return scene_fps


def _member_tensor_batch(
    frames: list[np.ndarray],
    hard: dict,
    output_width: int,
    output_height: int,
    member_count: int,
) -> torch.Tensor:
    width, height = _member_geometry(frames[0], hard, output_width, output_height, member_count)
    return torch.stack([_to_tensor(frame, width, height) for frame in frames], dim=0)


def decode_reference_images(reference_set) -> tuple:
    """Return the fixed r01..r16 IMAGE tuple for an image Reference lane."""
    context = _reference_decode_context(reference_set, "image")
    empty = _empty_image(context["width"], context["height"])
    if not context["present"] or "image_slots" not in context.get("live", set()):
        return tuple(empty for _ in range(MAX_REFERENCE_SLOTS))

    hard = context["hard"]
    records = list(context["records"])
    assembly = str(hard.get("assembly", "batch") or "batch")
    if assembly == "temporal":
        # Deliberate Sonder override of the surveyed separate-background socket:
        # context-class references occupy the final temporal segment.
        records.sort(key=lambda record: getattr(record["reference"], "reference_class", "") == "context")
    decode_span = assembly in {"batch", "slots"}
    target_fps = _reference_frame_rate(context)
    member_frames = [
        _load_member_images(record, decode_span=decode_span, target_fps=target_fps, hard=hard)
        for record in records
    ]
    member_tensors = [
        _member_tensor_batch(frames, hard, context["width"], context["height"], len(records))
        for frames in member_frames
    ]

    if assembly == "sheet":
        sheet_width, sheet_height = _member_geometry(
            member_frames[0][0], hard, context["width"], context["height"], len(records)
        )
        compositor = _strip if hard.get("layout") == "strip" else _sheet
        assembled = compositor(
            [frames[0] for frames in member_frames],
            sheet_width,
            sheet_height,
            str(hard.get("background", "black")),
        )
        loop_frames = max(0, _int(hard.get("loop_frames"), 0))
        if loop_frames:
            assembled = assembled.repeat(_grid_frame_count(hard, loop_frames), 1, 1, 1)
        values = [assembled]
    elif assembly == "temporal":
        step = max(1, _int(hard.get("frame_step"), 8))
        offset = max(0, _int(hard.get("frame_offset"), 1))
        count = len(member_tensors)
        required = step * count + offset
        allowed = sorted({
            max(1, _int(value, 0)) for value in (hard.get("allowed_frame_counts") or [])
            if _int(value, 0) > 0
        })
        safe_allowed = [value for value in allowed if value >= required]
        if allowed and not safe_allowed:
            raise RuntimeError(
                f"Reference temporal assembly needs at least {required} frames for {count} members, "
                f"but the recipe's largest allowed length is {allowed[-1]}."
            )
        requested = max(0, _int(context["item"].get("sequence_frames"), 0))
        if safe_allowed:
            frame_count = safe_allowed[0] if requested == 0 else min(
                safe_allowed, key=lambda value: (abs(value - requested), value)
            )
        else:
            frame_count = max(required, requested)
        blocks = max(count, (frame_count - offset) // step)
        sequence = torch.zeros(
            (frame_count, member_tensors[0].shape[1], member_tensors[0].shape[2], 3),
            dtype=torch.float32,
        )
        for index, tensor in enumerate(member_tensors):
            segment_start = (index * blocks // count) * step
            segment_end = frame_count if index == count - 1 else ((index + 1) * blocks // count) * step
            sequence[segment_start:segment_end] = tensor[0]
        values = [sequence]
    elif assembly == "slots":
        values = member_tensors
    else:
        ordered = list(member_tensors)
        if hard.get("primary_model_position") == "last" and len(ordered) > 1:
            ordered = [*ordered[1:], ordered[0]]
        # A custom batch+native recipe may still raise here for mixed geometry;
        # that is the pre-existing schema gap recorded by the plan.
        values = [torch.cat(ordered, dim=0)]

    return tuple(
        values[index] if index < len(values) else empty
        for index in range(MAX_REFERENCE_SLOTS)
    )


def decode_reference_audios(reference_set) -> tuple:
    """Return the fixed a01..a16 AUDIO tuple, one trimmed member per slot."""
    context = _reference_decode_context(reference_set, "audio")
    if not context["present"] or "audio_slots" not in context.get("live", set()):
        return tuple(_silent_audio() for _ in range(MAX_REFERENCE_SLOTS))
    values = [_audio_output(record) for record in context["records"]]
    return tuple(
        values[index] if index < len(values) else _silent_audio()
        for index in range(MAX_REFERENCE_SLOTS)
    )


def decode_reference_prompts(reference_set) -> tuple:
    """Return aggregate prompt/names followed by p01..p16."""
    context = _reference_decode_context(reference_set)
    if not context["present"]:
        return ("", "", *("" for _ in range(MAX_REFERENCE_SLOTS)))
    live = context["live"]
    records = context["records"]
    ref = context["ref"]
    registry = ref.get("registry") if isinstance(ref.get("registry"), dict) else None
    prompt, slot_prompts = _formatted_prompts(
        context["item"], records, context["recipe"], registry)
    names = ", ".join(reference_member_labels(
        getattr(record["reference"], "name", ""),
        getattr(record["member"], "name", ""),
    )["name"] for record in records)
    return (
        prompt if "reference_prompt" in live else "",
        names if "reference_names" in live else "",
        *(
            slot_prompts[index]
            if "reference_prompt" in live and index < len(slot_prompts)
            else ""
            for index in range(MAX_REFERENCE_SLOTS)
        ),
    )
