"""Reference selector and bridge execution core.

The selector side is intentionally metadata-only.  Media is resolved and
decoded only by :mod:`reference_bridge_v3` when the lazy reference branch is
actually evaluated.
"""

from __future__ import annotations

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
    fit_frame_to_canvas,
    resolve_source_color_interpretation,
)
from ..server.path_security import resolve_existing_project_path
from ..server.reference_resolution import reference_live_outputs, resolve_effective_references
from ..server.timeline_state import LaneConfig, ReferenceItem, ReferenceLaneRecipe


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
        pegs = {
            "frame_constraint": getattr(job, "frame_constraint", None),
            "dimension_constraint": getattr(job, "dimension_constraint", None),
        }
        source_name = "snapshot"
    else:
        lane_count = max(1, _int(getattr(scene, "reference_lane_count", 1), 1)) if scene else 1
        configs = list(getattr(scene, "reference_lane_configs", []) or []) if scene else []
        recipes = list(getattr(scene, "reference_lane_recipes", []) or []) if scene else []
        items = list(getattr(scene, "reference_items", []) or []) if scene else []
        threshold = _float((getattr(project, "metadata", {}) or {}).get("reference_frame_threshold", 0.0))
        pegs = {
            "frame_constraint": getattr(project, "frame_constraint", None),
            "dimension_constraint": getattr(project, "dimension_constraint", None),
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
    recipe = source["recipes"][lane_index] if lane_index < len(source["recipes"]) else ReferenceLaneRecipe()
    item_dict = selected.to_dict() if hasattr(selected, "to_dict") else dict(selected or {})
    recipe_dict = recipe.to_dict() if hasattr(recipe, "to_dict") else dict(recipe or {})
    return {
        "project": project,
        "source": source["source"],
        "lane_index": lane_index,
        "has_reference": int(bool(item_dict)),
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


def _load_member_image(record: dict[str, Any]) -> np.ndarray:
    asset, member, path = record["asset"], record["member"], record["path"]
    if getattr(asset, "asset_type", "") == "video":
        fps = max(0.001, _float(getattr(asset, "fps", 0.0), 24.0))
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
    mode = str(hard.get("output_size", "scene") or "scene")
    multiple = max(1, _int(hard.get("size_multiple"), 1))
    if mode == "native":
        height, width = frame.shape[:2]
        long_edge = min(max(width, height), max(16, _int(hard.get("long_edge_max"), 848)))
        scale = long_edge / max(1, max(width, height))
        return _snap_dimension(width * scale, multiple, ceiling=long_edge), _snap_dimension(height * scale, multiple, ceiling=long_edge)
    # A lone member may have its own canonical size (Best Face ID's bust crop is
    # ~460x406 where its 4-panel sheet is exactly 1536x1024).
    single = hard.get("single_member_size")
    if member_count <= 1 and isinstance(single, list) and len(single) >= 2:
        return max(1, _int(single[0], output_width)), max(1, _int(single[1], output_height))
    if mode == "custom" and _int(hard.get("width"), 0) and _int(hard.get("height"), 0):
        return _int(hard["width"]), _int(hard["height"])
    if multiple > 1:
        # Floor, never round up: exceeding the requested output is worse than
        # losing a few pixels (this is what VACE's /16 rule needs).
        return _snap_dimension(output_width, multiple, floor=True), _snap_dimension(output_height, multiple, floor=True)
    return output_width, output_height


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


def member_prompt_fragment(pattern: str, index: int, prompt: str, name: str) -> str:
    """Expand one member's slice of the derived prompt.

    A pattern may use `{n}` (1-based), `{index}` (0-based), `{prompt}` and
    `{name}` as many times as it likes, so a recipe can compose a sentence like
    `<Subject {n}> is {prompt}, from <Picture {n}>` rather than only prefixing a
    token. With no `{prompt}`/`{name}` placeholder the member text is appended
    after the expanded pattern, which is what the original `token: label` form
    did and is why existing recipes keep composing identically.
    """
    member_prompt = str(prompt or "").strip()
    entity_name = str(name or "").strip()
    label = member_prompt or entity_name
    if not pattern:
        return label
    expanded = (
        pattern.replace("{n}", str(index + 1))
        .replace("{index}", str(index))
        .replace("{prompt}", member_prompt)
        .replace("{name}", entity_name)
    )
    if "{prompt}" in pattern or "{name}" in pattern:
        return expanded.strip()
    return f"{expanded}: {label}" if label else expanded


def _assemble_prompt(item: dict, records: list[dict[str, Any]], recipe: dict) -> str:
    override = str(item.get("prompt_override", "") or "").strip()
    if override:
        return override
    soft = recipe.get("soft", {}) if isinstance(recipe.get("soft"), dict) else {}
    token_pattern = str(soft.get("prompt_tokens", "") or "")
    values = []
    for index, record in enumerate(records):
        fragment = member_prompt_fragment(
            token_pattern,
            index,
            getattr(record["member"], "prompt", ""),
            getattr(record["reference"], "name", ""),
        )
        if fragment:
            values.append(fragment)
    prefix = str(soft.get("prompt_prefix", "") or "").strip()
    joined = ", ".join(values)
    return " ".join(value for value in (prefix, joined) if value)


def _member_prompts(records: list[dict[str, Any]], recipe: dict) -> list[str]:
    """Per-slot prompt text, one entry per staged member, for p01..p16."""
    soft = recipe.get("soft", {}) if isinstance(recipe.get("soft"), dict) else {}
    pattern = str(soft.get("prompt_tokens", "") or "")
    return [
        member_prompt_fragment(
            pattern, index,
            getattr(record["member"], "prompt", ""),
            getattr(record["reference"], "name", ""),
        )
        for index, record in enumerate(records)
    ]


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


def _bridge_tuple(
    live: set,
    width: int,
    height: int,
    *,
    frames=None,
    reference_index: int = 0,
    strength: float = 0.0,
    audio=None,
    prompt: str = "",
    names: str = "",
    context=None,
    slots=None,
    slot_prompts=None,
) -> tuple:
    """Emit the frozen tuple, replacing every dead output with its fallback."""

    empty = _empty_image(width, height)
    slot_values = list(slots or []) if "slots" in live else []
    # p01..p16 follow the same liveness as reference_prompt: they are the same
    # composition, split per slot.
    prompt_values = list(slot_prompts or []) if "reference_prompt" in live else []
    return (
        frames if frames is not None and "reference_frames" in live else empty,
        int(reference_index) if "reference_idx" in live else 0,
        float(strength) if "reference_strength" in live else 0.0,
        audio if audio is not None and "reference_audio" in live else _silent_audio(),
        prompt if "reference_prompt" in live else "",
        names if "reference_names" in live else "",
        context if context is not None and "context" in live else empty,
        *[
            slot_values[index] if index < len(slot_values) else empty
            for index in range(MAX_REFERENCE_SLOTS)
        ],
        *[
            prompt_values[index] if index < len(prompt_values) else ""
            for index in range(MAX_REFERENCE_SLOTS)
        ],
    )


def decode_reference_set(reference_set) -> tuple:
    """Decode and assemble the frozen bridge tuple plus ``r01..r16``."""

    ref = reference_set if isinstance(reference_set, dict) else {}
    width, height = max(1, _int(ref.get("width"), 1280)), max(1, _int(ref.get("height"), 720))
    empty = _empty_image(width, height)
    if not ref.get("has_reference"):
        return _bridge_tuple(set(), width, height)
    project = ref.get("project")
    item = ref.get("item") if isinstance(ref.get("item"), dict) else {}
    recipe_wrapper = ref.get("recipe") if isinstance(ref.get("recipe"), dict) else {}
    recipe = recipe_wrapper.get("recipe") if isinstance(recipe_wrapper.get("recipe"), dict) else {}
    hard = resolve_pegged_hard(
        recipe.get("hard") if isinstance(recipe.get("hard"), dict) else {},
        ref.get("pegs") if isinstance(ref.get("pegs"), dict) else {},
    )
    records = _member_records(project, item)
    cap = max(1, _int(hard.get("max_members"), MAX_REFERENCE_SLOTS))
    if len(records) > min(cap, MAX_REFERENCE_SLOTS):
        raise RuntimeError(
            f"Reference recipe accepts at most {min(cap, MAX_REFERENCE_SLOTS)} members; {len(records)} were staged."
        )
    live = reference_live_outputs(hard)
    media_kind = "audio" if recipe_wrapper.get("media_kind") == "audio" else "image"
    names = ", ".join(str(getattr(record["reference"], "name", "") or "") for record in records)
    prompt = _assemble_prompt(item, records, recipe)
    local_index = max(0, _int(item.get("start_frame"), 0) - _int(ref.get("render_start"), 0))
    if media_kind == "audio":
        if len(records) != 1:
            raise RuntimeError("Audio reference recipes require exactly one member.")
        return _bridge_tuple(
            live, width, height,
            audio=_audio_output(records[0]), prompt=prompt, names=names,
        )

    images = [_load_member_image(record) for record in records]
    assembly = str(hard.get("assembly", "batch") or "batch")
    member_tensors = []
    for image in images:
        member_width, member_height = _member_geometry(image, hard, width, height, len(images))
        member_tensors.append(_to_tensor(image, member_width, member_height).unsqueeze(0))
    context = None
    temporal_tensors = list(member_tensors)
    if assembly == "temporal" and hard.get("context_slot"):
        context_index = next((
            index for index, record in enumerate(records)
            if getattr(record["reference"], "reference_class", "") == "context"
            or "sonder:location" in (getattr(record["member"], "tags", []) or [])
        ), None)
        if context_index is not None:
            context = member_tensors[context_index]
            temporal_tensors = [tensor for index, tensor in enumerate(member_tensors) if index != context_index]
        if not temporal_tensors:
            temporal_tensors = [empty]
    if assembly == "sheet":
        sheet_width, sheet_height = _member_geometry(images[0], hard, width, height, len(images))
        compositor = _strip if hard.get("layout") == "strip" else _sheet
        frames = compositor(images, sheet_width, sheet_height, str(hard.get("background", "black")))
        # Ingredients loops one static panel sheet as a reference video. This is
        # the assembled sequence length only — it never constrains the render
        # window, which the editor's own {step, offset} governs.
        loop_frames = max(0, _int(hard.get("loop_frames"), 0))
        if loop_frames:
            frames = frames.repeat(_grid_frame_count(hard, loop_frames), 1, 1, 1)
        reference_index = 0
    elif assembly == "temporal":
        # MSR assembles a frame sequence in which each reference occupies a
        # contiguous segment on the temporal VAE grid, the first starting at
        # index 0. Occupied length is the authored likeness-strength control, so
        # sparse single frames separated by black are not the same conditioning.
        step = max(1, _int(hard.get("frame_step"), 8))
        offset = max(0, _int(hard.get("frame_offset"), 1))
        count = len(temporal_tensors)
        allowed = sorted(max(1, _int(value, 0)) for value in (hard.get("allowed_frame_counts") or []))
        required = step * count + offset
        frame_count = next((value for value in allowed if value >= required), required)
        blocks = max(count, (frame_count - offset) // step)
        sequence = torch.zeros(
            (frame_count, temporal_tensors[0].shape[1], temporal_tensors[0].shape[2], 3),
            dtype=torch.float32,
        )
        for index, tensor in enumerate(temporal_tensors):
            segment_start = (index * blocks // count) * step
            segment_end = frame_count if index == count - 1 else ((index + 1) * blocks // count) * step
            sequence[segment_start:segment_end] = tensor[0]
        frames = sequence
        reference_index = local_index
    elif assembly == "slots":
        frames = member_tensors[0]
        reference_index = 0
    else:
        # Batch recipes require uniform shapes. Recipe geometry guarantees it.
        ordered_tensors = list(member_tensors)
        if hard.get("primary_model_position") == "last" and len(ordered_tensors) > 1:
            ordered_tensors = [*ordered_tensors[1:], ordered_tensors[0]]
        frames = torch.cat(ordered_tensors, dim=0)
        reference_index = 0
    return _bridge_tuple(
        live, width, height,
        frames=frames, reference_index=reference_index, strength=1.0,
        prompt=prompt, names=names, context=context,
        slots=member_tensors[:MAX_REFERENCE_SLOTS],
        slot_prompts=_member_prompts(records, recipe)[:MAX_REFERENCE_SLOTS],
    )
