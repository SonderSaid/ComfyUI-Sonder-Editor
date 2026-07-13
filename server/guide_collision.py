"""Pure guide/driver temporal-coordinate collision helpers.

LTX-style guide nodes append latent frames and identify them through temporal
RoPE coordinates. Duplicate coordinates make downstream guide-count cropping
undercount appended latents. Drivers retain their content-defined positions;
single-image guides may be shifted to a free pixel-frame coordinate.
"""

from __future__ import annotations

import math
from typing import Any


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _constraint(frame_constraint: dict | None) -> tuple[int, int] | None:
    if not isinstance(frame_constraint, dict):
        return None
    step = max(1, _as_int(frame_constraint.get("step"), 1))
    if step <= 1:
        return None
    return step, _as_int(frame_constraint.get("offset"), 0)


def _snap_pixel(pixel: int, frame_constraint: dict | None, side: str) -> int:
    resolved = _constraint(frame_constraint)
    if resolved is None:
        return pixel
    step, offset = resolved
    if pixel <= 0:
        return 0
    if pixel < offset:
        return 0 if side == "start" else offset
    ratio = (pixel - offset) / step
    if side == "start":
        return offset + math.floor(ratio) * step
    return offset + math.ceil(ratio) * step


def _snap_mask_pre(value: int, actual_pre: int, step: int) -> int:
    value = max(0, min(_as_int(value), actual_pre))
    if value >= actual_pre:
        return actual_pre
    return min(actual_pre, math.ceil(value / step) * step)


def _snap_mask_post(value: int, actual_post: int, step: int) -> int:
    value = max(0, min(_as_int(value), actual_post))
    return min(actual_post, math.ceil(value / step) * step)


def round_up_frame_count(frame_count: int, frame_constraint: dict | None) -> int:
    count = max(0, _as_int(frame_count))
    if count <= 0 or not isinstance(frame_constraint, dict) or "step" not in frame_constraint:
        return count
    step = max(1, _as_int(frame_constraint.get("step"), 1))
    offset = _as_int(frame_constraint.get("offset"), 0)
    minimum = max(1, _as_int(frame_constraint.get("min"), 1))
    count = max(count, minimum)
    if (count - offset) % step == 0:
        return count
    return offset + math.ceil((count - offset) / step) * step


def resolve_execution_window(
    *,
    scene_duration: int,
    selection_start: int,
    selection_end: int,
    pre_context_frames: int = 0,
    post_context_frames: int = 0,
    mask_pre_offset: int = 0,
    mask_post_offset: int = 0,
    frame_constraint: dict | None = None,
) -> dict:
    """Mirror the editor's authoritative constraint-aware render-window math."""

    duration = max(0, _as_int(scene_duration))
    generation_start = max(0, min(duration, _as_int(selection_start)))
    generation_end = max(generation_start, min(duration, _as_int(selection_end)))
    actual_pre = min(max(0, _as_int(pre_context_frames)), generation_start)
    actual_post = min(max(0, _as_int(post_context_frames)), duration - generation_end)

    resolved = _constraint(frame_constraint)
    if resolved is not None:
        step, _offset = resolved
        if actual_pre > 0:
            aligned_pre = _snap_pixel(actual_pre, frame_constraint, "end")
            extension = max(0, aligned_pre - actual_pre)
            if extension <= generation_start - actual_pre:
                actual_pre += extension
        post_remainder = actual_post % step
        extension = (step - post_remainder) % step
        if extension <= duration - generation_end - actual_post:
            actual_post += extension
        mask_pre_offset = _snap_mask_pre(mask_pre_offset, actual_pre, step)
        mask_post_offset = _snap_mask_post(mask_post_offset, actual_post, step)
    else:
        mask_pre_offset = max(0, min(_as_int(mask_pre_offset), actual_pre))
        mask_post_offset = max(0, min(_as_int(mask_post_offset), actual_post))

    render_start = generation_start - actual_pre
    render_end = generation_end + actual_post
    source_frame_count = max(0, render_end - render_start)
    frame_count = round_up_frame_count(source_frame_count, frame_constraint)
    return {
        "generation_start": generation_start,
        "generation_end": generation_end,
        "render_start": render_start,
        "render_end": render_end,
        "actual_pre": actual_pre,
        "actual_post": actual_post,
        "mask_pre_offset": mask_pre_offset,
        "mask_post_offset": mask_post_offset,
        "source_frame_count": source_frame_count,
        "frame_count": frame_count,
        "frame_count_padding": max(0, frame_count - source_frame_count),
    }


def snap_driver_start(idx: int, step: int, offset: int) -> int:
    idx = _as_int(idx)
    step = max(1, _as_int(step, 1))
    offset = _as_int(offset)
    if idx <= 0:
        return 0
    return max(0, offset + ((idx - offset) // step) * step)


def driver_occupied_coords(local_idx: int, pixel_len: int, step: int, offset: int) -> list[int]:
    local_idx = _as_int(local_idx)
    pixel_len = _as_int(pixel_len)
    step = max(1, _as_int(step, 1))
    offset = _as_int(offset)
    if pixel_len <= 1 or step <= 1:
        return [local_idx]
    start = snap_driver_start(local_idx, step, offset)
    if start == 0:
        count = max(0, math.ceil((pixel_len - 1) / step))
        return [0] + [offset + step * k for k in range(count)]
    count = max(1, math.ceil(pixel_len / step))
    return [start + step * k for k in range(count)]


def resolve_guide_collisions(
    *,
    guides: list[dict] | None,
    drivers: list[dict] | None,
    frame_count: int,
    frame_constraint: dict | None,
    auto_offset_enabled: bool = True,
) -> dict:
    """Return deterministic effective guide coordinates and collision counts."""

    frame_count = max(0, _as_int(frame_count))
    resolved = _constraint(frame_constraint)
    entries: list[dict] = []
    driver_entries: list[dict] = []
    if resolved is None or frame_count <= 0:
        seen_ids: set[str] = set()
        for index, guide in enumerate(guides or []):
            guide_id = str(guide.get("guide_id") or f"legacy-guide-{index}")
            if guide_id in seen_ids:
                guide_id = f"{guide_id}#{index}"
            seen_ids.add(guide_id)
            original = _as_int(guide.get("local_idx"))
            entries.append({
                "guide_id": guide_id,
                "bridge_override_key": str(guide.get("bridge_override_key") or ""),
                "original_local_idx": original,
                "effective_local_idx": original,
                "collided": False,
                "collided_with": "",
            })
        return {
            "driver_coords": driver_entries,
            "entries": entries,
            "collision_count": 0,
            "driver_driver_collision_count": 0,
            "unresolved_collision_count": 0,
            "predicted_unresolved": False,
            "max_excess_latents": 0,
        }

    step, offset = resolved
    effective_occupied: dict[int, str] = {}
    original_counts: dict[int, int] = {}
    driver_driver_collision_count = 0

    sorted_drivers = sorted(
        (dict(item) for item in (drivers or []) if isinstance(item, dict)),
        key=lambda item: (_as_int(item.get("local_idx")), _as_int(item.get("lane_index")), str(item.get("clip_id") or "")),
    )
    for index, driver in enumerate(sorted_drivers):
        clip_id = str(driver.get("clip_id") or f"driver-{index}")
        coords = driver_occupied_coords(
            driver.get("local_idx"), driver.get("pixel_len"), step, offset
        )
        label = f"driver:{clip_id}"
        for coord in coords:
            if original_counts.get(coord, 0) > 0:
                driver_driver_collision_count += 1
            original_counts[coord] = original_counts.get(coord, 0) + 1
            effective_occupied.setdefault(coord, label)
        driver_entries.append({
            "clip_id": clip_id,
            "lane_index": _as_int(driver.get("lane_index")),
            "local_idx": _as_int(driver.get("local_idx")),
            "snapped_start": snap_driver_start(driver.get("local_idx"), step, offset),
            "pixel_len": _as_int(driver.get("pixel_len")),
            "coords": sorted(set(coords)),
        })

    normalized_guides: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(guides or []):
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        guide_id = str(item.get("guide_id") or f"legacy-guide-{index}")
        if guide_id in seen_ids:
            guide_id = f"{guide_id}#{index}"
        seen_ids.add(guide_id)
        item["guide_id"] = guide_id
        normalized_guides.append(item)
    normalized_guides.sort(key=lambda item: (_as_int(item.get("local_idx")), item["guide_id"]))

    original_guide_collision_count = 0
    for guide in normalized_guides:
        original = _as_int(guide.get("local_idx"))
        if original_counts.get(original, 0) > 0:
            original_guide_collision_count += 1
        original_counts[original] = original_counts.get(original, 0) + 1

        effective = original
        collided_with = effective_occupied.get(effective, "")
        collided = bool(collided_with)
        if collided:
            candidate = effective
            while candidate in effective_occupied and candidate < frame_count:
                candidate += 1
            if candidate >= frame_count:
                candidate = original - 1
                while candidate in effective_occupied and candidate >= 0:
                    candidate -= 1
            if candidate < 0:
                if auto_offset_enabled:
                    raise ValueError("No free frame coordinate remains for guide auto-offset")
                candidate = original
            effective = candidate
        effective_occupied.setdefault(effective, f"guide:{guide['guide_id']}")
        entries.append({
            "guide_id": guide["guide_id"],
            "bridge_override_key": str(guide.get("bridge_override_key") or ""),
            "original_local_idx": original,
            "effective_local_idx": effective,
            "collided": collided,
            "collided_with": collided_with,
        })

    collision_count = sum(1 for entry in entries if entry["collided"])
    unresolved = driver_driver_collision_count
    if not auto_offset_enabled:
        unresolved += original_guide_collision_count
    return {
        "driver_coords": driver_entries,
        "entries": entries,
        "collision_count": collision_count,
        "driver_driver_collision_count": driver_driver_collision_count,
        "unresolved_collision_count": unresolved,
        "predicted_unresolved": unresolved > 0,
        "max_excess_latents": unresolved,
    }


def check_frame_count_excess(expected: int, actual: int, step: int, max_excess_latents: int) -> bool:
    expected = _as_int(expected)
    actual = _as_int(actual)
    step = _as_int(step, 1)
    maximum = max(0, _as_int(max_excess_latents))
    excess = actual - expected
    return step > 1 and maximum > 0 and excess > 0 and excess % step == 0 and excess // step <= maximum
