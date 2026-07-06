from __future__ import annotations

import logging
import os
import uuid

import cv2
import numpy as np
import torch

from .atomic_io import atomic_replace
from .media_helpers import (
    apply_rgb_color_correction,
    color_correction_for_interpretation,
    fit_frame_to_canvas,
    get_ffmpeg_path,
    resolve_source_color_interpretation,
    run_ffmpeg_command,
)
from .path_security import resolve_existing_project_path, resolve_project_path
from .timeline_state import Scene, TimelineProject

# Composited render caches are invalidated when color handling changes;
# bump this salt alongside any change to the decode color pipeline.
_RENDER_CACHE_COLOR_VERSION = "cm1"


def _default_video_capture(path: str):
    # Pin the FFmpeg backend: the color-correction constants (fixed BT.601
    # matrix assumption) were measured against it. Fall back to the single-arg
    # form for VideoCapture substitutes that only accept a path.
    try:
        return cv2.VideoCapture(path, cv2.CAP_FFMPEG)
    except TypeError:
        return cv2.VideoCapture(path)

logger = logging.getLogger("sonder_editor")


class TimelineRenderCancelled(RuntimeError):
    """Raised when a timeline render or mixdown is cancelled."""


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TimelineRenderCancelled("timeline export cancelled")


def _resolve_project_media_path(project: TimelineProject, source_path: str) -> str:
    return resolve_existing_project_path(
        project,
        source_path,
        purpose="timeline media source",
    )


def _scene_resolution(project: TimelineProject, scene: Scene) -> tuple[int, int]:
    proj_w, proj_h = project.resolution
    if getattr(scene, "width", 0) > 0:
        proj_w = int(scene.width)
    if getattr(scene, "height", 0) > 0:
        proj_h = int(scene.height)
    return max(1, int(proj_w or 1)), max(1, int(proj_h or 1))


def _visible_render_clips(scene: Scene) -> list:
    hidden_lanes = {
        idx
        for idx, cfg in enumerate(getattr(scene, "video_lane_configs", []) or [])
        if getattr(cfg, "hidden", False)
    }
    return [
        clip
        for clip in getattr(scene, "clips", []) or []
        if getattr(clip, "track_index", 0) not in hidden_lanes
        and getattr(clip, "role", "render") == "render"
        and not getattr(clip, "muted", False)
    ]


def iter_scene_frames(
    project: TimelineProject,
    scene: Scene,
    start_frame: int,
    end_frame: int,
    *,
    cancel_event=None,
    video_capture_factory=None,
):
    """Yield uncached uint8 RGB scene frames for [start_frame, end_frame)."""
    _check_cancel(cancel_event)
    video_capture_factory = video_capture_factory or _default_video_capture
    proj_w, proj_h = _scene_resolution(project, scene)
    render_start = max(0, int(start_frame or 0))
    render_end = max(render_start, int(end_frame or render_start))

    visible_clips = _visible_render_clips(scene)
    captures = {}

    def get_cap(source_path: str):
        abs_path = _resolve_project_media_path(project, source_path)
        if not abs_path:
            logger.info("Skipping render clip %s: file not found or quarantined", source_path)
            return None
        if abs_path not in captures:
            cap = video_capture_factory(abs_path)
            if cap.isOpened():
                asset = project.asset_for_source_path(source_path)
                interpretation = resolve_source_color_interpretation(asset, abs_path)
                captures[abs_path] = (cap, color_correction_for_interpretation(interpretation))
            else:
                logger.warning("Cannot open video: %s", abs_path)
                return None
        return captures[abs_path]

    try:
        for frame_index in range(render_start, render_end):
            _check_cancel(cancel_event)
            canvas = np.zeros((proj_h, proj_w, 3), dtype=np.uint8)
            active = [
                clip
                for clip in visible_clips
                if clip.timeline_start_frame <= frame_index < clip.timeline_end_frame
            ]
            active.sort(key=lambda clip: clip.track_index)

            for clip in active:
                _check_cancel(cancel_event)
                capture_entry = get_cap(clip.source_path)
                if capture_entry is None:
                    continue
                cap, color_affine = capture_entry
                source_frame = clip.source_in_frame + (frame_index - clip.timeline_start_frame)
                cap.set(cv2.CAP_PROP_POS_FRAMES, source_frame)
                ok, frame_bgr = cap.read()
                if not ok:
                    continue

                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                frame_rgb = apply_rgb_color_correction(frame_rgb, color_affine)
                placed, (dx, dy, dw, dh) = fit_frame_to_canvas(
                    frame_rgb, proj_w, proj_h,
                    mode=getattr(clip, "fit_mode", "pad_edge"),
                    crop_position=getattr(clip, "crop_position", "center"),
                )
                if getattr(clip, "opacity", 1.0) >= 1.0:
                    canvas[dy:dy + dh, dx:dx + dw] = placed[dy:dy + dh, dx:dx + dw]
                else:
                    opacity = float(getattr(clip, "opacity", 1.0) or 1.0)
                    roi_canvas = canvas[dy:dy + dh, dx:dx + dw]
                    roi_placed = placed[dy:dy + dh, dx:dx + dw]
                    canvas[dy:dy + dh, dx:dx + dw] = cv2.addWeighted(
                        roi_canvas,
                        1.0 - opacity,
                        roi_placed,
                        opacity,
                        0,
                    )
            yield np.ascontiguousarray(canvas)
    finally:
        for cap, _affine in captures.values():
            cap.release()


def render_scene_frames(
    project: TimelineProject,
    scene: Scene,
    start_frame: int,
    end_frame: int,
    *,
    cancel_event=None,
    video_capture_factory=None,
    use_cache: bool = True,
) -> torch.Tensor:
    """Composite visible render clips into an RGB float tensor for [start_frame, end_frame)."""
    _check_cancel(cancel_event)
    video_capture_factory = video_capture_factory or _default_video_capture
    proj_w, proj_h = _scene_resolution(project, scene)
    render_start = max(0, int(start_frame or 0))
    render_end = max(render_start, int(end_frame or render_start))
    num_frames = render_end - render_start

    if num_frames <= 0:
        return torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)

    cache_path = ""
    content_hash = ""
    if use_cache:
        content_hash = scene.content_hash(render_start, render_end, project.resolution)
        cache_filename = f"{scene.scene_id}_{content_hash}_{_RENDER_CACHE_COLOR_VERSION}.pt"
        cache_path = resolve_project_path(
            project,
            os.path.join("cache", "renders", cache_filename),
            purpose="timeline render cache file",
        )

    if use_cache and cache_path and os.path.isfile(cache_path):
        _check_cancel(cancel_event)
        try:
            cached = torch.load(cache_path, weights_only=True)
            logger.info("Render cache hit for scene %s (%d frames)", scene.scene_id, num_frames)
            return cached
        except Exception as exc:
            logger.warning("Failed to load render cache: %s", exc)
            try:
                os.remove(cache_path)
                logger.warning("Deleted corrupt render cache: %s", cache_path)
            except OSError as remove_error:
                logger.warning("Failed to delete corrupt render cache %s: %s", cache_path, remove_error)

    if not _visible_render_clips(scene):
        return torch.zeros(num_frames, proj_h, proj_w, 3, dtype=torch.float32)

    frames = list(iter_scene_frames(
        project,
        scene,
        render_start,
        render_end,
        cancel_event=cancel_event,
        video_capture_factory=video_capture_factory,
    ))
    arr = np.stack(frames, axis=0).astype(np.float32) / 255.0
    tensor = torch.from_numpy(arr)
    if not use_cache:
        return tensor

    cache_dir = resolve_project_path(
        project,
        os.path.join("cache", "renders"),
        purpose="timeline render cache root",
    )
    tmp_path = ""
    if cache_dir and cache_path:
        tmp_path = resolve_project_path(
            project,
            os.path.join("cache", "renders", f".{scene.scene_id}_{content_hash}_{_RENDER_CACHE_COLOR_VERSION}_{uuid.uuid4().hex[:8]}.tmp"),
            purpose="timeline render cache temp file",
        )
    if not cache_dir or not cache_path or not tmp_path:
        return tensor
    os.makedirs(cache_dir, exist_ok=True)
    try:
        _check_cancel(cancel_event)
        torch.save(tensor, tmp_path)
        _check_cancel(cancel_event)
        atomic_replace(tmp_path, cache_path)
        logger.info("Cached render for scene %s (%d frames)", scene.scene_id, num_frames)
    except TimelineRenderCancelled:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise
    except Exception as exc:
        try:
            if os.path.isfile(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        logger.warning("Failed to save render cache: %s", exc)

    return tensor


def _effective_scene_fps(project: TimelineProject, scene: Scene) -> float:
    fps = float(getattr(scene, "fps", 0.0) or getattr(project, "fps", 24.0) or 24.0)
    return max(0.001, fps)


def _audio_contributors(
    project: TimelineProject,
    scene: Scene,
    start_frame: int,
    end_frame: int,
) -> list[dict]:
    fps = _effective_scene_fps(project, scene)
    hidden_lanes = {
        idx
        for idx, cfg in enumerate(getattr(scene, "audio_lane_configs", []) or [])
        if getattr(cfg, "hidden", False)
    }
    contributors = []
    for track in getattr(scene, "audio_tracks", []) or []:
        if getattr(track, "muted", False):
            continue
        if getattr(track, "lane_index", 0) in hidden_lanes:
            continue
        overlap_start = max(start_frame, int(track.timeline_start_frame or 0))
        overlap_end = min(end_frame, int(track.timeline_end_frame or 0))
        if overlap_end <= overlap_start:
            continue
        source_path = _resolve_project_media_path(project, track.source_path)
        if not os.path.isfile(source_path):
            logger.info("Skipping export audio track %s: file not found", track.source_path)
            continue
        source_start = int(track.source_in_frame or 0) + (overlap_start - int(track.timeline_start_frame or 0))
        contributors.append({
            "track": track,
            "path": source_path,
            "source_start_sec": max(0.0, source_start / fps),
            "duration_sec": max(0.0, (overlap_end - overlap_start) / fps),
            "delay_ms": max(0, int(round(((overlap_start - start_frame) / fps) * 1000.0))),
            "volume": float(getattr(track, "volume", 1.0) if getattr(track, "volume", 1.0) is not None else 1.0),
        })
    return contributors


def mix_scene_audio_to_wav(
    project: TimelineProject,
    scene: Scene,
    start_frame: int,
    end_frame: int,
    output_wav: str,
    *,
    cancel_event=None,
) -> list[dict]:
    """Mix scene audio tracks into a stereo 44.1kHz WAV for [start_frame, end_frame)."""
    _check_cancel(cancel_event)
    fps = _effective_scene_fps(project, scene)
    duration_sec = max(0.001, (max(start_frame, end_frame) - start_frame) / fps)
    contributors = _audio_contributors(project, scene, start_frame, end_frame)
    os.makedirs(os.path.dirname(output_wav), exist_ok=True)

    if not contributors:
        cmd = [
            get_ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t",
            f"{duration_sec:.6f}",
            "-c:a",
            "pcm_s16le",
            str(output_wav),
        ]
        run_ffmpeg_command(cmd, timeout=max(30, int(duration_sec) + 30), cancel_event=cancel_event)
        _check_cancel(cancel_event)
        return []

    cmd = [get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y"]
    for entry in contributors:
        cmd += [
            "-ss",
            f"{entry['source_start_sec']:.6f}",
            "-t",
            f"{entry['duration_sec']:.6f}",
            "-i",
            str(entry["path"]),
        ]
    silence_index = len(contributors)
    cmd += [
        "-f",
        "lavfi",
        "-t",
        f"{duration_sec:.6f}",
        "-i",
        "anullsrc=channel_layout=stereo:sample_rate=44100",
    ]

    filters = []
    labels = []
    for idx, entry in enumerate(contributors):
        label = f"a{idx}"
        labels.append(f"[{label}]")
        delay = int(entry["delay_ms"])
        filters.append(
            f"[{idx}:a]"
            "asetpts=PTS-STARTPTS,"
            f"volume={entry['volume']:.6f},"
            f"adelay={delay}|{delay}"
            f"[{label}]"
        )
    silence_label = "silence"
    filters.append(f"[{silence_index}:a]anull[{silence_label}]")
    mix_inputs = labels + [f"[{silence_label}]"]
    filters.append(
        "".join(mix_inputs)
        + f"amix=inputs={len(mix_inputs)}:duration=longest,"
        + f"volume={len(mix_inputs):.6f},"
        + "aformat=sample_fmts=s16:channel_layouts=stereo[mix]"
    )
    cmd += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[mix]",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        "-t",
        f"{duration_sec:.6f}",
        str(output_wav),
    ]
    run_ffmpeg_command(cmd, timeout=max(30, int(duration_sec) + 60), cancel_event=cancel_event)
    _check_cancel(cancel_event)
    return contributors
