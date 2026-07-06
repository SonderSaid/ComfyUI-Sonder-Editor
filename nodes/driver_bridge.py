"""Sonder Driver selector and bridge nodes.

The selector resolves one Driver lane against the current Sonder execution
context without decoding media. The bridge consumes that resolved reference and
only decodes when its branch actually executes.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import numpy as np
import torch

from ..server.media_helpers import decode_video_range, fit_frame_to_canvas
from ..server.path_security import path_within, resolve_existing_project_path, resolve_project_path
from ..server.timeline_state import ClipReference, GuideFrame, LaneConfig

logger = logging.getLogger(__name__)


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _snapshot_version(queue_job) -> int:
    params = getattr(queue_job, "params", {}) or {}
    if not isinstance(params, dict):
        return 0
    return max(0, _coerce_int(params.get("snapshot_version", 0), 0))


def _find_ref_job(project):
    ctx = getattr(project, "_execution_context", None) or {}
    job_id = str(ctx.get("queue_job_ref_id", "") or "")
    if not job_id:
        return None
    for job in getattr(project, "generation_queue", []) or []:
        if getattr(job, "job_id", "") == job_id:
            return job
    return None


def _resolve_active_scene(project):
    ctx = getattr(project, "_execution_context", None) or {}
    scene_id = ctx.get("scene_id", "")
    if scene_id:
        scene = project.get_scene(scene_id)
        if scene is not None:
            return scene
    scenes = getattr(project, "scenes", None) or []
    return scenes[0] if scenes else None


def _resolve_render_window(project, scene):
    proj_w, proj_h = getattr(project, "resolution", (1280, 720))
    if scene is not None:
        if getattr(scene, "width", 0) and scene.width > 0:
            proj_w = scene.width
        if getattr(scene, "height", 0) and scene.height > 0:
            proj_h = scene.height
    ctx = getattr(project, "_execution_context", None) or {}
    render_start = ctx.get("context_start")
    render_end = ctx.get("context_end")
    if render_start is None or render_end is None:
        render_start = 0
        render_end = getattr(scene, "duration_frames", 0) if scene is not None else 0
    return int(render_start), int(render_end), int(proj_w), int(proj_h)


def _resolve_output_frame_count(project, render_start: int, render_end: int) -> int:
    ctx = getattr(project, "_execution_context", None) or {}
    frame_count = _coerce_int(ctx.get("frame_count"), render_end - render_start)
    return max(1, frame_count)


def _resolve_frame_constraint(project):
    ctx = getattr(project, "_execution_context", None) or {}
    constraint = ctx.get("frame_constraint")
    if isinstance(constraint, dict) and constraint:
        return constraint
    constraint = getattr(project, "frame_constraint", None)
    return constraint if isinstance(constraint, dict) and constraint else None


def _lane_hidden(configs, lane_index: int) -> bool:
    if lane_index < 0 or lane_index >= len(configs):
        return False
    cfg = configs[lane_index]
    if isinstance(cfg, dict):
        return bool(cfg.get("hidden", False))
    return bool(getattr(cfg, "hidden", False))


def _snapshot_lane_configs(queue_job, lane_count: int) -> list[LaneConfig]:
    configs = [
        LaneConfig.from_dict(item) if isinstance(item, dict) else item
        for item in (getattr(queue_job, "driver_lane_configs", []) or [])
    ]
    while len(configs) < lane_count:
        configs.append(LaneConfig())
    return configs[:lane_count]


def _resolve_driver_source(project, scene):
    queue_job = _find_ref_job(project)
    if queue_job is not None and _snapshot_version(queue_job) > 0:
        lane_count = max(1, _coerce_int(getattr(queue_job, "driver_lane_count", 1), 1))
        return {
            "source": "snapshot",
            "clips": [
                ClipReference.from_dict(item)
                for item in (getattr(queue_job, "driver_clip_snapshots", []) or [])
                if isinstance(item, dict)
            ],
            "lane_count": lane_count,
            "lane_configs": _snapshot_lane_configs(queue_job, lane_count),
            "guides": [
                GuideFrame.from_dict(item)
                for item in (getattr(queue_job, "guide_frame_snapshots", []) or [])
                if isinstance(item, dict)
            ],
            "guide_track_hidden": False,
        }

    lane_count = max(1, _coerce_int(getattr(scene, "motion_driver_lane_count", 1), 1)) if scene else 1
    configs = list(getattr(scene, "motion_driver_lane_configs", []) or []) if scene else []
    while len(configs) < lane_count:
        configs.append(LaneConfig())
    return {
        "source": "live",
        "clips": list(getattr(scene, "clips", []) or []) if scene else [],
        "lane_count": lane_count,
        "lane_configs": configs[:lane_count],
        "guides": list(getattr(scene, "guide_frames", []) or []) if scene else [],
        "guide_track_hidden": bool(getattr(getattr(scene, "guide_track_config", None), "hidden", False)) if scene else False,
    }


def _parse_bridge_overrides(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        data = raw
    elif isinstance(raw, str):
        try:
            data = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    else:
        return {}
    if not isinstance(data, dict):
        return {}
    drivers = data.get("drivers")
    return drivers if isinstance(drivers, dict) else data


def _override_muted_value(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, dict) and "muted" in value:
        return bool(value.get("muted"))
    return None


def _driver_override_state(raw_overrides, lane_index: int) -> str:
    overrides = _parse_bridge_overrides(raw_overrides)
    for key in (f"lane:{lane_index}", str(lane_index)):
        muted = _override_muted_value(overrides.get(key))
        if muted is True:
            return "mute"
        if muted is False:
            return "on"
    return "inherit"


def _reserved_guide_indices(project, scene, source_info, render_start: int, render_end: int) -> set[int]:
    if source_info.get("guide_track_hidden"):
        return set()
    reserved = set()
    duration = max(0, _coerce_int(getattr(scene, "duration_frames", 0), 0)) if scene else 0
    for guide in source_info.get("guides", []) or []:
        if bool(getattr(guide, "muted", False)):
            continue
        idx = _coerce_int(getattr(guide, "frame_index", 0), 0)
        if idx == -1:
            idx = max(0, duration - 1)
        if render_start <= idx < render_end:
            reserved.add(idx - render_start)
    return reserved


def _valid_fallback_indices(frame_count: int, frame_constraint: dict | None) -> list[int]:
    frame_count = max(1, _coerce_int(frame_count, 1))
    if not frame_constraint:
        return list(range(frame_count))
    step = max(1, _coerce_int(frame_constraint.get("step"), 1))
    if step <= 1:
        return list(range(frame_count))
    offset = _coerce_int(frame_constraint.get("offset"), 0)
    valid = {0}
    cursor = offset
    while cursor < 0:
        cursor += step
    while cursor < frame_count:
        valid.add(cursor)
        cursor += step
    return sorted(valid)


def fallback_driver_index(frame_count: int, frame_constraint: dict | None, reserved_indices=None) -> int:
    reserved = set(reserved_indices or [])
    valid = _valid_fallback_indices(frame_count, frame_constraint)
    for idx in reversed(valid):
        if idx not in reserved:
            return idx
    return valid[-1] if valid else 0


def _empty_image(width: int, height: int) -> torch.Tensor:
    return torch.zeros(1, max(1, height), max(1, width), 3, dtype=torch.float32)


def _project_dir_value(project_or_dir) -> str:
    if isinstance(project_or_dir, str):
        return project_or_dir
    return getattr(project_or_dir, "project_dir", "") or ""


def _projects_base_dir() -> str:
    try:
        import folder_paths

        base = os.path.join(folder_paths.get_output_directory(), "sonder-projects")
    except Exception:
        return ""
    return os.path.realpath(base) if base else ""


def _validated_driver_project_dir(project_dir: str) -> str:
    raw = str(project_dir or "")
    if not raw or "\x00" in raw:
        return ""
    base_dir = _projects_base_dir()
    if not base_dir:
        return ""
    project_real = os.path.realpath(raw)
    return project_real if path_within(base_dir, project_real) else ""


def _abs_driver_path(project_or_dir, source_path: str) -> str:
    return resolve_existing_project_path(
        _project_dir_value(project_or_dir),
        source_path,
        purpose="driver media source",
    )


def _driver_path_is_contained(project_or_dir, source_path: str) -> bool:
    return bool(resolve_project_path(
        _project_dir_value(project_or_dir),
        source_path,
        purpose="driver media source",
    ))


def _decode_driver_clip(project, clip, overlap_start: int, overlap_len: int, width: int, height: int) -> torch.Tensor:
    source_start = _coerce_int(getattr(clip, "source_in_frame", 0), 0) + (
        overlap_start - _coerce_int(getattr(clip, "timeline_start_frame", 0), 0)
    )
    source_path = str(getattr(clip, "source_path", "") or "")
    abs_path = _abs_driver_path(project, source_path)
    if not os.path.isfile(abs_path):
        raise RuntimeError(f"Driver media not found: {source_path or abs_path}")

    frames = []
    try:
        decoded = decode_video_range(abs_path, source_start, source_start + overlap_len)
        for offset in range(overlap_len):
            try:
                frame_rgb = next(decoded)
            except StopIteration as exc:
                frame_no = source_start + offset
                raise RuntimeError(
                    f"Driver media ended before expected frame {frame_no}: {source_path}"
                ) from exc
            placed, _bounds = fit_frame_to_canvas(
                frame_rgb,
                width,
                height,
                mode=getattr(clip, "fit_mode", "pad_edge"),
                crop_position=getattr(clip, "crop_position", "center"),
            )
            frames.append(placed)
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to decode driver media {source_path}: {exc}") from exc

    if not frames:
        raise RuntimeError(f"Driver media produced no frames: {source_path}")
    return torch.from_numpy(np.stack(frames, axis=0).astype(np.float32) / 255.0)


def _clip_to_dict(clip) -> dict[str, Any]:
    if isinstance(clip, dict):
        return dict(clip)
    if hasattr(clip, "to_dict") and callable(clip.to_dict):
        return clip.to_dict()
    return {
        "clip_id": getattr(clip, "clip_id", ""),
        "source_path": getattr(clip, "source_path", ""),
        "timeline_start_frame": _coerce_int(getattr(clip, "timeline_start_frame", 0), 0),
        "timeline_end_frame": _coerce_int(getattr(clip, "timeline_end_frame", 0), 0),
        "source_in_frame": _coerce_int(getattr(clip, "source_in_frame", 0), 0),
        "source_out_frame": _coerce_int(getattr(clip, "source_out_frame", 0), 0),
        "track_index": _coerce_int(getattr(clip, "track_index", 0), 0),
        "role": getattr(clip, "role", "render"),
        "strength": _coerce_float(getattr(clip, "strength", 1.0), 1.0),
        "muted": bool(getattr(clip, "muted", False)),
        "fit_mode": getattr(clip, "fit_mode", "pad_edge"),
        "crop_position": getattr(clip, "crop_position", "center"),
    }


def _base_driver_ref(
    project,
    *,
    source_info: dict,
    lane_index: int,
    render_start: int,
    render_end: int,
    width: int,
    height: int,
    frame_count: int,
    frame_constraint,
    reserved_guides,
    fallback_idx: int,
) -> dict[str, Any]:
    return {
        "schema": "sonder_driver_ref_v1",
        "source": source_info.get("source", "live"),
        "project_dir": getattr(project, "project_dir", "") or "",
        "lane_index": int(lane_index),
        "lane_count": max(0, _coerce_int(source_info.get("lane_count"), 0)),
        "render_start": int(render_start),
        "render_end": int(render_end),
        "width": int(width),
        "height": int(height),
        "frame_count": int(frame_count),
        "frame_constraint": frame_constraint if isinstance(frame_constraint, dict) else None,
        "reserved_guide_indices": sorted(int(idx) for idx in set(reserved_guides or [])),
        "fallback_idx": int(fallback_idx),
        "driver_idx": int(fallback_idx),
        "strength": 0.0,
        "has_driver": 0,
        "clip": None,
        "overlap_start": 0,
        "overlap_len": 0,
        "overlap_local_idx": int(fallback_idx),
    }


def resolve_driver_ref(project, driver_lane_index=0, driver_selector_overrides_json="{}") -> dict[str, Any]:
    """Resolve Driver lane metadata without decoding media."""

    scene = _resolve_active_scene(project)
    render_start, render_end, width, height = _resolve_render_window(project, scene)
    frame_count = _resolve_output_frame_count(project, render_start, render_end)
    source_info = _resolve_driver_source(project, scene)
    frame_constraint = _resolve_frame_constraint(project)
    reserved_guides = _reserved_guide_indices(project, scene, source_info, render_start, render_end)
    fallback_idx = fallback_driver_index(frame_count, frame_constraint, reserved_guides)
    lane_index = _coerce_int(driver_lane_index, 0)
    ref = _base_driver_ref(
        project,
        source_info=source_info,
        lane_index=lane_index,
        render_start=render_start,
        render_end=render_end,
        width=width,
        height=height,
        frame_count=frame_count,
        frame_constraint=frame_constraint,
        reserved_guides=reserved_guides,
        fallback_idx=fallback_idx,
    )

    lane_count = max(0, _coerce_int(source_info.get("lane_count"), 0))
    if lane_index < 0 or lane_index >= lane_count or render_end <= render_start:
        return ref

    lane_clips = [
        clip for clip in source_info.get("clips", []) or []
        if getattr(clip, "role", "render") == "motion_driver"
        and _coerce_int(getattr(clip, "track_index", 0), 0) == lane_index
    ]
    if not lane_clips:
        return ref
    if len(lane_clips) > 1:
        raise RuntimeError(
            f"Multiple driver clips exist on driver lane {lane_index}; keep one driver clip per lane."
        )
    clip = lane_clips[0]

    overlap_start = max(_coerce_int(getattr(clip, "timeline_start_frame", 0), 0), render_start)
    overlap_end = min(_coerce_int(getattr(clip, "timeline_end_frame", 0), 0), render_end)
    overlap_len = max(0, overlap_end - overlap_start)
    if overlap_len <= 0:
        return ref

    override_state = _driver_override_state(driver_selector_overrides_json, lane_index)
    if override_state == "mute":
        return ref

    editor_absent = (
        _lane_hidden(source_info.get("lane_configs", []), lane_index)
        or bool(getattr(clip, "muted", False))
    )
    if editor_absent and override_state != "on":
        return ref
    if not _driver_path_is_contained(project, getattr(clip, "source_path", "") or ""):
        return ref

    local_idx = int(overlap_start - render_start)
    ref.update({
        "has_driver": 1,
        "clip": _clip_to_dict(clip),
        "overlap_start": int(overlap_start),
        "overlap_len": int(overlap_len),
        "overlap_local_idx": local_idx,
        "driver_idx": local_idx,
        "strength": _coerce_float(getattr(clip, "strength", 1.0), 1.0),
    })
    return ref


def decode_driver_ref(driver_ref) -> dict[str, Any]:
    ref = driver_ref if isinstance(driver_ref, dict) else {}
    width = max(1, _coerce_int(ref.get("width"), 1))
    height = max(1, _coerce_int(ref.get("height"), 1))
    if _coerce_int(ref.get("has_driver"), 0) != 1:
        idx = _coerce_int(ref.get("fallback_idx", ref.get("driver_idx")), 0)
        return {
            "images": _empty_image(width, height),
            "idx": int(idx),
            "strength": 0.0,
            "has_driver": 0,
            "source": ref.get("source", "live"),
        }

    clip_data = ref.get("clip")
    if not isinstance(clip_data, dict):
        raise RuntimeError("Driver ref is marked present but contains no clip metadata.")
    overlap_len = _coerce_int(ref.get("overlap_len"), 0)
    if overlap_len <= 0:
        raise RuntimeError("Driver ref is marked present but contains no positive overlap.")

    clip = ClipReference.from_dict(clip_data)
    project_dir = _validated_driver_project_dir(str(ref.get("project_dir", "") or ""))
    if not project_dir:
        raise RuntimeError("Driver project directory is outside the configured project base.")

    images = _decode_driver_clip(
        project_dir,
        clip,
        _coerce_int(ref.get("overlap_start"), 0),
        overlap_len,
        width,
        height,
    )
    return {
        "images": images,
        "idx": _coerce_int(ref.get("overlap_local_idx", ref.get("driver_idx")), 0),
        "strength": _coerce_float(ref.get("strength"), 1.0),
        "has_driver": 1,
        "source": ref.get("source", "live"),
    }


def resolve_driver_bridge(project, driver_lane_index=0, driver_bridge_overrides_json="{}") -> dict[str, Any]:
    """Compatibility helper that resolves and decodes in one call."""

    return decode_driver_ref(resolve_driver_ref(project, driver_lane_index, driver_bridge_overrides_json))


class SonderDriverSelector:
    """Resolve one Driver lane and expose presence for lazy graph routing."""

    CATEGORY = "Sonder"
    RETURN_TYPES = ("SONDER_DRIVER_REF", "INT")
    RETURN_NAMES = ("driver_ref", "has_driver")
    OUTPUT_TOOLTIPS = (
        "Resolved Driver reference for the selected lane. Wire to Sonder Driver Bridge.",
        "0 when no effective driver is present, 1 when the selected lane contributes media.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Resolves a selected Driver lane without decoding media. Use has_driver "
        "to drive Sonder Switch/Cluster and pass driver_ref to Sonder Driver Bridge "
        "inside the active driver branch."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project": ("SONDER_PROJECT", {
                    "tooltip": "Wire from the Sonder Editor project output.",
                }),
            },
            "optional": {
                "driver_lane_index": ("INT", {
                    "default": 0,
                    "min": 0,
                    "max": 999,
                    "tooltip": "Zero-based Driver lane order. Missing lanes are treated as no driver.",
                }),
                "driver_selector_overrides_json": ("STRING", {
                    "default": "{}",
                    "tooltip": "Workflow-local Driver selector override state.",
                }),
            },
        }

    def execute(self, project, driver_lane_index=0, driver_selector_overrides_json="{}"):
        ref = resolve_driver_ref(project, driver_lane_index, driver_selector_overrides_json)
        ctx = getattr(project, "_execution_context", None)
        if isinstance(ctx, dict):
            ctx["driver_selector"] = {
                "lane_index": _coerce_int(driver_lane_index, 0),
                "has_driver": int(ref["has_driver"]),
                "driver_idx": int(ref["driver_idx"]),
                "strength": float(ref["strength"]),
                "source": ref.get("source", "live"),
            }
        logger.info(
            "driver selector: lane=%s source=%s has_driver=%s idx=%s strength=%.4f",
            driver_lane_index,
            ref.get("source", "live"),
            ref["has_driver"],
            ref["driver_idx"],
            ref["strength"],
        )
        return (ref, int(ref["has_driver"]))


class SonderDriverBridge:
    """Decode Driver media from a resolved Driver reference."""

    CATEGORY = "Sonder"
    RETURN_TYPES = ("IMAGE", "INT", "FLOAT")
    RETURN_NAMES = ("driver_images", "driver_idx", "driver_strength")
    OUTPUT_TOOLTIPS = (
        "Driver frames for the selected driver lane, window-rebased to the editor output.",
        "Local output frame where the driver segment starts, or a deterministic fallback index.",
        "Driver conditioning strength. Emits 0 when no driver is active.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Consumes a Sonder Driver Selector reference. Missing lanes/clips emit a "
        "black fallback; decode failures for active drivers raise a node error."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "driver_ref": ("SONDER_DRIVER_REF", {
                    "tooltip": "Wire from Sonder Driver Selector.",
                }),
            },
        }

    def execute(self, driver_ref):
        result = decode_driver_ref(driver_ref)
        lane_index = _coerce_int(driver_ref.get("lane_index"), 0) if isinstance(driver_ref, dict) else 0
        logger.info(
            "driver bridge: lane=%s source=%s has_driver=%s idx=%s strength=%.4f",
            lane_index,
            result.get("source", "live"),
            result["has_driver"],
            result["idx"],
            result["strength"],
        )
        return (
            result["images"],
            int(result["idx"]),
            float(result["strength"]),
        )
