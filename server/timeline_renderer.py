from __future__ import annotations

import logging
import os
import uuid

import cv2
import numpy as np
import torch

from .media_helpers import fit_frame_to_canvas, get_ffmpeg_path, run_ffmpeg_command
from .timeline_state import Scene, TimelineProject

logger = logging.getLogger("sonder_editor")


class TimelineRenderCancelled(RuntimeError):
    """Raised when a timeline render or mixdown is cancelled."""


def _check_cancel(cancel_event) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise TimelineRenderCancelled("timeline export cancelled")


def _resolve_project_media_path(project: TimelineProject, source_path: str) -> str:
    source_path = str(source_path or "")
    if source_path and os.path.isfile(source_path):
        return source_path
    return os.path.join(project.project_dir, source_path)


def _scene_resolution(project: TimelineProject, scene: Scene) -> tuple[int, int]:
    proj_w, proj_h = project.resolution
    if getattr(scene, "width", 0) > 0:
        proj_w = int(scene.width)
    if getattr(scene, "height", 0) > 0:
        proj_h = int(scene.height)
    return max(1, int(proj_w or 1)), max(1, int(proj_h or 1))


def render_scene_frames(
    project: TimelineProject,
    scene: Scene,
    start_frame: int,
    end_frame: int,
    *,
    cancel_event=None,
    video_capture_factory=None,
) -> torch.Tensor:
    """Composite visible render clips into an RGB float tensor for [start_frame, end_frame)."""
    _check_cancel(cancel_event)
    video_capture_factory = video_capture_factory or cv2.VideoCapture
    proj_w, proj_h = _scene_resolution(project, scene)
    render_start = max(0, int(start_frame or 0))
    render_end = max(render_start, int(end_frame or render_start))
    num_frames = render_end - render_start

    if num_frames <= 0:
        return torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)

    cache_dir = os.path.join(project.project_dir, "cache", "renders")
    content_hash = scene.content_hash(render_start, render_end, project.resolution)
    cache_path = os.path.join(cache_dir, f"{scene.scene_id}_{content_hash}.pt")

    if os.path.isfile(cache_path):
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

    hidden_lanes = {
        idx
        for idx, cfg in enumerate(getattr(scene, "video_lane_configs", []) or [])
        if getattr(cfg, "hidden", False)
    }
    visible_clips = [
        clip
        for clip in getattr(scene, "clips", []) or []
        if getattr(clip, "track_index", 0) not in hidden_lanes
        and getattr(clip, "role", "render") == "render"
        and not getattr(clip, "muted", False)
    ]

    if not visible_clips:
        return torch.zeros(num_frames, proj_h, proj_w, 3, dtype=torch.float32)

    captures = {}

    def get_cap(source_path: str):
        abs_path = _resolve_project_media_path(project, source_path)
        if abs_path not in captures:
            cap = video_capture_factory(abs_path)
            if cap.isOpened():
                captures[abs_path] = cap
            else:
                logger.warning("Cannot open video: %s", abs_path)
                return None
        return captures[abs_path]

    try:
        frames: list[np.ndarray] = []
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
                cap = get_cap(clip.source_path)
                if cap is None:
                    continue
                source_frame = clip.source_in_frame + (frame_index - clip.timeline_start_frame)
                cap.set(cv2.CAP_PROP_POS_FRAMES, source_frame)
                ok, frame_bgr = cap.read()
                if not ok:
                    continue

                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                placed, (dx, dy, dw, dh) = fit_frame_to_canvas(frame_rgb, proj_w, proj_h)
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
            frames.append(canvas)

        arr = np.stack(frames, axis=0).astype(np.float32) / 255.0
        tensor = torch.from_numpy(arr)

        os.makedirs(cache_dir, exist_ok=True)
        tmp_path = os.path.join(cache_dir, f".{scene.scene_id}_{content_hash}_{uuid.uuid4().hex[:8]}.tmp")
        try:
            _check_cancel(cancel_event)
            torch.save(tensor, tmp_path)
            _check_cancel(cancel_event)
            os.replace(tmp_path, cache_path)
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
    finally:
        for cap in captures.values():
            cap.release()


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
        cmd += ["-i", str(entry["path"])]

    filters = []
    labels = []
    for idx, entry in enumerate(contributors):
        label = f"a{idx}"
        labels.append(f"[{label}]")
        delay = int(entry["delay_ms"])
        filters.append(
            f"[{idx}:a]"
            f"atrim=start={entry['source_start_sec']:.6f}:duration={entry['duration_sec']:.6f},"
            "asetpts=PTS-STARTPTS,"
            f"volume={entry['volume']:.6f},"
            f"adelay={delay}|{delay}"
            f"[{label}]"
        )
    if len(labels) == 1:
        filters.append(
            f"{labels[0]}"
            + f"atrim=duration={duration_sec:.6f},"
            + "aformat=sample_fmts=s16:channel_layouts=stereo[mix]"
        )
    else:
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=longest,"
            + f"volume={len(labels):.6f},"
            + f"atrim=duration={duration_sec:.6f},"
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
