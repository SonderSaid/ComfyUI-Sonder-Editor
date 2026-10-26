from __future__ import annotations

import logging
import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass

import cv2
import numpy as np
import torch

from .media_helpers import (
    apply_rgb_color_correction,
    color_correction_for_interpretation,
    fit_frame_to_canvas,
    resolve_source_color_interpretation,
)
from .path_security import resolve_existing_project_path
from .render_cache import (
    CACHE_FORMAT_VERSION,
    CACHE_PIPELINE_VERSION,
    RenderCacheError,
    active_cache_store,
    cache_store,
    discard_staged,
    enforce_render_cache_budget,
    load_block,
    prepare_store,
    publish_staged,
    stage_block,
)
from .timeline_state import Scene, TimelineProject, effective_scene_fps
from .lane_registry import hidden_lane_indexes

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
        raise TimelineRenderCancelled("timeline render cancelled")


# A reader up to this many frames behind its target decodes forward instead of
# seeking. OpenCV's FFmpeg seek steps back from the target and decodes forward
# to it, so even an all-intra FFV1 seek decodes several frames: Phase 0 measured
# 5 ms (640px H.264) to 1.4 s (2560x1536 FFV1) per seek, against at most 2.2 ms
# per grab, so 16 grabs cost less than one seek on every measured codec.
SEQUENTIAL_GRAB_LIMIT = 16
# A reader no clip has used for more than this many canvas frames is released,
# so a long export does not keep one decoder per source it ever touched.
READER_IDLE_RELEASE_FRAMES = 48
# Decoders of one source open at once. Layers beyond this share a reader and seek.
MAX_READERS_PER_SOURCE = 4


def _release_capture(cap) -> None:
    # A failing release must not replace the exception a render is unwinding with.
    try:
        cap.release()
    except Exception:
        logger.warning("Releasing a video capture failed", exc_info=True)


class _SourceReader:
    """One open capture and where its decoder stands.

    ``next_pos`` is the frame the next ``read()`` returns, or None when unknown
    (after a failed seek, grab or read), which forces a seek. A fresh capture
    that reports frame 0 starts there, so a clip near its source's start skips
    the seek, which alone costs about a second on a large FFV1 file. The last
    decoded frame is kept so a repeated index (rate up-conversion, the end clamp,
    two layers on one frame) costs no decode.
    """

    __slots__ = ("cap", "next_pos", "last_index", "last_bgr", "last_used")

    def __init__(self, cap, frame_index):
        self.cap = cap
        self.next_pos = 0 if self._reports_start(cap) else None
        self.last_index = None
        self.last_bgr = None
        self.last_used = frame_index

    @staticmethod
    def _reports_start(cap) -> bool:
        get = getattr(cap, "get", None)
        if not callable(get):
            return False
        try:
            return get(cv2.CAP_PROP_POS_FRAMES) == 0
        except Exception:
            return False

    def holds(self, target: int) -> bool:
        return self.last_index == target and self.last_bgr is not None

    def can_reach(self, target: int) -> bool:
        return self.next_pos is not None and self.next_pos <= target <= self.next_pos + SEQUENTIAL_GRAB_LIMIT

    def _lose_position(self) -> None:
        self.next_pos = None
        self.last_index = None
        self.last_bgr = None

    def _keep(self, target: int, frame_bgr: np.ndarray) -> np.ndarray:
        self.next_pos = target + 1
        self.last_index = target
        self.last_bgr = frame_bgr
        return frame_bgr

    def read(self, target: int, mark) -> np.ndarray | None:
        """The BGR frame at decode index ``target``, or None after marking why."""
        if self.holds(target):
            return self.last_bgr
        if self.can_reach(target):
            grabbed = all(self.cap.grab() for _ in range(target - self.next_pos))
            ok, frame_bgr = self.cap.read() if grabbed else (False, None)
            if ok:
                return self._keep(target, frame_bgr)
            # The block stays out of the cache, but the frame gets the seek the
            # old seek-every-frame path would have made.
            self._lose_position()
            mark("read_failed")
        seek_failed = self.cap.set(cv2.CAP_PROP_POS_FRAMES, target) is False
        if seek_failed:
            mark("seek_failed")
        ok, frame_bgr = self.cap.read()
        if not ok:
            self._lose_position()
            mark("read_failed")
            return None
        if seek_failed:
            # The frame is used as before, but nothing is known about where the
            # decoder stands, so the next request seeks again.
            self._lose_position()
            return frame_bgr
        return self._keep(target, frame_bgr)


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
    hidden_lanes = hidden_lane_indexes(scene, "video")
    return [
        clip
        for clip in getattr(scene, "clips", []) or []
        if getattr(clip, "track_index", 0) not in hidden_lanes
        and getattr(clip, "role", "render") == "render"
        and not getattr(clip, "muted", False)
    ]


@dataclass(frozen=True)
class _PreparedSource:
    source_path: str
    abs_path: str
    resolved_identity: str
    revision: str
    source_fps: float
    native_frame_count: int
    interpretation: tuple[str, str] | None
    color_affine: np.ndarray | None
    rate_ratio: float

    def fingerprint_payload(self) -> dict:
        return {
            "source_path": self.source_path,
            "resolved_identity": self.resolved_identity,
            "revision": self.revision,
            "source_fps": _float_identity(self.source_fps),
            "native_frame_count": self.native_frame_count,
            "interpretation": list(self.interpretation) if self.interpretation is not None else None,
        }


def _source_revision(path: str) -> str:
    if not path:
        return ""
    try:
        stat = os.stat(path)
        return f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return ""


def _normalized_source_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(str(value or ""))).replace("\\", "/")


def _float_identity(value: float) -> str:
    return float(value).hex()


def _prepare_sources(project: TimelineProject, scene: Scene, clips: list) -> dict[str, _PreparedSource]:
    scene_fps = effective_scene_fps(project, scene)
    prepared = {}
    for clip in clips:
        source_path = str(getattr(clip, "source_path", "") or "")
        if source_path in prepared:
            continue
        abs_path = _resolve_project_media_path(project, source_path)
        asset_lookup = getattr(project, "asset_for_source_path", None)
        asset = asset_lookup(source_path) if callable(asset_lookup) else None
        try:
            source_fps = float(getattr(asset, "fps", 0.0) or 0.0) if asset else 0.0
        except (TypeError, ValueError, OverflowError):
            source_fps = 0.0
        if not math.isfinite(source_fps) or source_fps <= 0:
            source_fps = 0.0
        try:
            native_frame_count = int(float(getattr(asset, "frame_count", 0) or 0)) if asset else 0
        except (TypeError, ValueError, OverflowError):
            native_frame_count = 0
        native_frame_count = max(0, native_frame_count)
        interpretation = resolve_source_color_interpretation(asset, abs_path) if abs_path else None
        prepared[source_path] = _PreparedSource(
            source_path=_normalized_source_path(source_path),
            abs_path=abs_path,
            resolved_identity=os.path.normcase(os.path.realpath(abs_path)) if abs_path else "",
            revision=_source_revision(abs_path),
            source_fps=source_fps,
            native_frame_count=native_frame_count,
            interpretation=interpretation,
            color_affine=color_correction_for_interpretation(interpretation),
            rate_ratio=(source_fps / scene_fps) if source_fps > 0 else 1.0,
        )
    return prepared


def _current_source_state(source: _PreparedSource) -> tuple[str, str]:
    if not source.abs_path:
        return "", ""
    return os.path.normcase(os.path.realpath(source.abs_path)), _source_revision(source.abs_path)


def _source_revisions_changed(prepared: dict[str, _PreparedSource]) -> set[str]:
    return {
        source_path
        for source_path, source in prepared.items()
        if _current_source_state(source) != (source.resolved_identity, source.revision)
    }


def _effective_opacity_payload(clip) -> dict:
    raw = getattr(clip, "opacity", 1.0)
    if raw >= 1.0:
        return {"mode": "replace"}
    return {"mode": "blend", "value": float(raw or 1.0)}


def _block_contributors(visible_clips: list, block_start: int, block_end: int,
                        prepared: dict[str, _PreparedSource]) -> list[dict]:
    contributors = []
    overlapping = []
    for clip in visible_clips:
        clip_start = int(getattr(clip, "timeline_start_frame", 0) or 0)
        clip_end = int(getattr(clip, "timeline_end_frame", 0) or 0)
        overlap_start = max(block_start, clip_start)
        overlap_end = min(block_end, clip_end)
        if overlap_end <= overlap_start:
            continue
        overlapping.append((clip, clip_start, overlap_start, overlap_end))
    overlapping.sort(key=lambda item: int(getattr(item[0], "track_index", 0) or 0))
    for order, (clip, clip_start, overlap_start, overlap_end) in enumerate(overlapping):
        source_path = str(getattr(clip, "source_path", "") or "")
        source = prepared.get(source_path)
        contributors.append({
            "order": order,
            "overlap": [overlap_start, overlap_end],
            "source_frame_at_overlap": int(getattr(clip, "source_in_frame", 0) or 0) + overlap_start - clip_start,
            "opacity": _effective_opacity_payload(clip),
            "fit_mode": str(getattr(clip, "fit_mode", "pad_edge") or "pad_edge"),
            "crop_position": str(getattr(clip, "crop_position", "center") or "center"),
            "source": source.fingerprint_payload() if source else {
                "source_path": _normalized_source_path(source_path),
                "resolved_identity": "",
                "revision": "",
                "source_fps": "0",
                "native_frame_count": 0,
                "interpretation": None,
            },
        })
    return contributors


def _block_fingerprint(*, block_index: int, block_start: int, block_end: int,
                       width: int, height: int, scene_fps: float,
                       contributors: list[dict]) -> str:
    payload = {
        "format": CACHE_FORMAT_VERSION,
        "pipeline": CACHE_PIPELINE_VERSION,
        "block_index": block_index,
        "bounds": [block_start, block_end],
        "width": width,
        "height": height,
        "scene_fps": _float_identity(scene_fps),
        "contributors": contributors,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _copy_uint8_slice_to_output(output: torch.Tensor, frames: torch.Tensor,
                                *, block_start: int, request_start: int, request_end: int) -> None:
    copy_start = max(block_start, request_start)
    copy_end = min(block_start + int(frames.shape[0]), request_end)
    if copy_end <= copy_start:
        return
    source_slice = frames[copy_start - block_start:copy_end - block_start]
    target = output[copy_start - request_start:copy_end - request_start]
    target.copy_(source_slice)
    target.div_(255.0)


def _consecutive_runs(indices: list[int]) -> list[list[int]]:
    runs = []
    for index in sorted(indices):
        if not runs or index != runs[-1][-1] + 1:
            runs.append([index])
        else:
            runs[-1].append(index)
    return runs


def iter_scene_frames(
    project: TimelineProject,
    scene: Scene,
    start_frame: int,
    end_frame: int,
    *,
    cancel_event=None,
    video_capture_factory=None,
    prepared_sources: dict[str, _PreparedSource] | None = None,
    render_status: dict | None = None,
    cache_block_frames: int = 1,
):
    """Yield uncached uint8 RGB scene frames for [start_frame, end_frame)."""
    _check_cancel(cancel_event)
    video_capture_factory = video_capture_factory or _default_video_capture
    proj_w, proj_h = _scene_resolution(project, scene)
    render_start = max(0, int(start_frame or 0))
    render_end = max(render_start, int(end_frame or render_start))

    visible_clips = _visible_render_clips(scene)
    scene_fps = effective_scene_fps(project, scene)
    prepared_sources = prepared_sources or _prepare_sources(project, scene, visible_clips)
    # Readers per capture key. Several readers of one source exist only while
    # clips of that source are composited at different positions at once.
    readers: dict[tuple, list[_SourceReader]] = {}
    failed_captures = set()
    # Keys whose extra reader failed to open: their layers share existing readers.
    reader_limited = set()

    if render_status is not None:
        render_status.setdefault("failed_blocks", set())
        render_status.setdefault("failures", [])

    def mark_failure(frame_index: int, source_path: str, reason: str) -> None:
        if render_status is None:
            return
        block_index = max(0, int(frame_index)) // max(1, int(cache_block_frames or 1))
        render_status["failed_blocks"].add(block_index)
        if len(render_status["failures"]) < 64:
            render_status["failures"].append((block_index, source_path, reason))

    def capture_key_for(source_path: str, frame_index: int):
        source = prepared_sources.get(source_path)
        abs_path = source.abs_path if source else ""
        if not abs_path:
            logger.info("Skipping render clip %s: file not found or quarantined", source_path)
            mark_failure(frame_index, source_path, "missing_or_quarantined")
            return None, None
        capture_key = (
            abs_path,
            source.source_fps if source else 0.0,
            source.native_frame_count if source else 0,
            source.interpretation if source else None,
        )
        if capture_key in failed_captures:
            mark_failure(frame_index, source_path, "open_failed")
            return None, None
        return capture_key, source

    def open_reader(capture_key, source, source_path: str, frame_index: int):
        pool = readers.setdefault(capture_key, [])
        cap = video_capture_factory(source.abs_path)
        if cap.isOpened():
            reader = _SourceReader(cap, frame_index)
            pool.append(reader)
            return reader
        logger.warning("Cannot open video: %s", source.abs_path)
        _release_capture(cap)
        if pool:
            # A reader of this source already works: share it rather than drop
            # the layer, and stop opening more for this render.
            reader_limited.add(capture_key)
            return pool[0]
        failed_captures.add(capture_key)
        mark_failure(frame_index, source_path, "open_failed")
        return None

    def assign_readers(requests: list, frame_index: int) -> None:
        """Give each clip request the reader that reaches its frame most cheaply.

        Cached frames and in-reach readers are handed out before any reader is
        taken to seek, so a cut on one layer never steals the reader another
        layer of the same source is continuing on.
        """
        taken = set()
        # (key, target) -> the reader that will hold that frame this canvas frame,
        # so a second layer on the same source frame shares one decode.
        serving = {}

        def give(request, reader):
            request["reader"] = reader
            taken.add(reader)
            serving[(request["key"], request["target"])] = reader

        for request in requests:
            if request["key"] is None:
                continue
            for reader in readers.get(request["key"], ()):
                if reader.holds(request["target"]):
                    give(request, reader)
                    break
        for request in requests:
            if request["key"] is None or request["reader"] is not None:
                continue
            shared = serving.get((request["key"], request["target"]))
            if shared is not None:
                request["reader"] = shared
                continue
            reachable = [reader for reader in readers.get(request["key"], ())
                         if reader not in taken and reader.can_reach(request["target"])]
            if reachable:
                give(request, min(reachable, key=lambda reader: request["target"] - reader.next_pos))
        for request in requests:
            if request["key"] is None or request["reader"] is not None:
                continue
            shared = serving.get((request["key"], request["target"]))
            if shared is not None:
                request["reader"] = shared
                continue
            pool = readers.get(request["key"], [])
            free = [reader for reader in pool if reader not in taken]
            if free:
                request["reader"] = free[0]
            elif pool and (len(pool) >= MAX_READERS_PER_SOURCE or request["key"] in reader_limited):
                # Reading happens in track order, so a shared reader serves the
                # earlier layer before it moves for this one.
                request["reader"] = pool[0]
            else:
                request["reader"] = open_reader(request["key"], request["source"], request["clip"].source_path,
                                                frame_index)
            if request["reader"] is not None:
                give(request, request["reader"])

    def retire_idle_readers(frame_index: int) -> None:
        for capture_key, pool in list(readers.items()):
            kept = []
            for reader in pool:
                idle = frame_index - reader.last_used
                if idle > READER_IDLE_RELEASE_FRAMES:
                    _release_capture(reader.cap)
                    continue
                if idle > 1:
                    # Only a frame used on the previous canvas frame can repeat.
                    reader.last_bgr = None
                    reader.last_index = None
                kept.append(reader)
            if kept:
                readers[capture_key] = kept
            else:
                readers.pop(capture_key, None)

    try:
        for frame_index in range(render_start, render_end):
            _check_cancel(cancel_event)
            retire_idle_readers(frame_index)
            canvas = np.zeros((proj_h, proj_w, 3), dtype=np.uint8)
            active = [
                clip
                for clip in visible_clips
                if clip.timeline_start_frame <= frame_index < clip.timeline_end_frame
            ]
            active.sort(key=lambda clip: clip.track_index)

            requests = []
            for clip in active:
                capture_key, source = capture_key_for(clip.source_path, frame_index)
                target = None
                if capture_key is not None:
                    source_units = clip.source_in_frame + (frame_index - clip.timeline_start_frame)
                    target = int(math.floor((source_units + 0.5) * source.rate_ratio))
                    if source.native_frame_count > 0:
                        target = min(source.native_frame_count - 1, max(0, target))
                requests.append({"clip": clip, "key": capture_key, "source": source, "target": target,
                                 "reader": None})
            assign_readers(requests, frame_index)

            for request in requests:
                _check_cancel(cancel_event)
                clip, reader = request["clip"], request["reader"]
                if reader is None:
                    continue
                reader.last_used = frame_index
                frame_bgr = reader.read(
                    request["target"],
                    lambda reason, clip=clip: mark_failure(frame_index, clip.source_path, reason),
                )
                if frame_bgr is None:
                    continue
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                frame_rgb = apply_rgb_color_correction(frame_rgb, request["source"].color_affine)
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
        for pool in readers.values():
            for reader in pool:
                _release_capture(reader.cap)


def render_scene_frames(
    project: TimelineProject,
    scene: Scene,
    start_frame: int,
    end_frame: int,
    *,
    cancel_event=None,
    video_capture_factory=None,
    use_cache: bool = True,
    cache_max_bytes: int | None = None,
    progress=None,
) -> torch.Tensor:
    """Composite visible render clips into an RGB float tensor for [start_frame, end_frame).

    ``progress(done, total)``, when given, starts at ``(0, total)`` and counts
    requested frames as they are composited, copied from the cache or found
    empty; ``total`` is the requested frame count. Frames a cache block composes
    outside the request are not counted, so ``done`` never passes ``total``. A
    retried attempt starts again at 0. An exception raised by the callback
    (an interrupt) propagates after captures are released and staged blocks
    discarded.
    """
    _check_cancel(cancel_event)
    video_capture_factory = video_capture_factory or _default_video_capture
    proj_w, proj_h = _scene_resolution(project, scene)
    render_start = max(0, int(start_frame or 0))
    render_end = max(render_start, int(end_frame or render_start))
    declared_duration = max(0, int(getattr(scene, "duration_frames", 0) or 0))
    if declared_duration > 0:
        render_start = min(render_start, declared_duration)
        render_end = min(max(render_start, render_end), declared_duration)
    num_frames = render_end - render_start

    if num_frames <= 0:
        return torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)

    visible_clips = _visible_render_clips(scene)
    if not visible_clips and not use_cache:
        return torch.zeros(num_frames, proj_h, proj_w, 3, dtype=torch.float32)

    if not use_cache:
        logger.info(
            "Render cache outcome=disabled scene=%s range=%d-%d budget=%s",
            getattr(scene, "scene_id", ""),
            render_start,
            render_end,
            0,
        )
        frame_iter = iter_scene_frames(
            project,
            scene,
            render_start,
            render_end,
            cancel_event=cancel_event,
            video_capture_factory=video_capture_factory,
        )
        frames = []
        if progress is not None:
            progress(0, num_frames)
        try:
            for frame in frame_iter:
                frames.append(frame)
                if progress is not None:
                    progress(len(frames), num_frames)
        finally:
            # Release the captures now, not when an exception's traceback is freed.
            frame_iter.close()
        arr = np.stack(frames, axis=0).astype(np.float32) / 255.0
        return torch.from_numpy(arr)

    scene_fps = effective_scene_fps(project, scene)
    store_failure_reason = ""
    try:
        store = cache_store(project, scene.scene_id, proj_w, proj_h, scene_fps)
    except (OSError, RenderCacheError) as exc:
        store_failure_reason = str(exc)
        store = None
    if store is None:
        logger.warning(
            "Render cache outcome=store_unavailable scene=%s range=%d-%d reason=%s",
            scene.scene_id,
            render_start,
            render_end,
            store_failure_reason or "no_store",
        )
        return render_scene_frames(
            project,
            scene,
            render_start,
            render_end,
            cancel_event=cancel_event,
            video_capture_factory=video_capture_factory,
            use_cache=False,
            progress=progress,
        )

    store_activity = active_cache_store(store)
    store_activity.__enter__()
    scene_duration = max(render_end, int(getattr(scene, "duration_frames", 0) or 0))
    try:
        prepare_store(store, scene_duration)
    except Exception as exc:
        store_activity.__exit__(None, None, None)
        logger.warning(
            "Render cache outcome=prepare_failed scene=%s root=%s store=%s "
            "block_frames=%d range=%d-%d reason=%s",
            scene.scene_id,
            store.root,
            store.token,
            store.block_frames,
            render_start,
            render_end,
            exc,
        )
        return render_scene_frames(
            project,
            scene,
            render_start,
            render_end,
            cancel_event=cancel_event,
            video_capture_factory=video_capture_factory,
            use_cache=False,
            progress=progress,
        )

    block_frames = store.block_frames
    first_block = render_start // block_frames
    last_block = (render_end - 1) // block_frames
    requested_block_indices = list(range(first_block, last_block + 1))
    coverage_start = first_block * block_frames
    coverage_end = min(scene_duration, (last_block + 1) * block_frames)
    coverage_clips = [
        clip for clip in visible_clips
        if int(getattr(clip, "timeline_start_frame", 0) or 0) < coverage_end
        and int(getattr(clip, "timeline_end_frame", 0) or 0) > coverage_start
    ]
    request_id = uuid.uuid4().hex
    logger.info(
        "Render cache outcome=ready scene=%s root=%s store=%s block_frames=%d "
        "range=%d-%d requested_blocks=%s budget=%s",
        scene.scene_id,
        store.root,
        store.token,
        block_frames,
        render_start,
        render_end,
        requested_block_indices,
        "unlimited" if cache_max_bytes is None else cache_max_bytes,
    )

    def render_attempt(staged):
        _check_cancel(cancel_event)
        done = 0

        def count_requested(first: int, end: int) -> None:
            nonlocal done
            added = max(0, min(end, render_end) - max(first, render_start))
            if added and progress is not None:
                done += added
                progress(done, num_frames)

        if progress is not None:
            progress(0, num_frames)
        output = torch.zeros(num_frames, proj_h, proj_w, 3, dtype=torch.float32)
        prepared = _prepare_sources(project, scene, coverage_clips)
        block_info = {}
        misses = []
        delete_indices = set()
        hit_count = 0
        hit_indices = []
        staging_failed = False

        for block_index in requested_block_indices:
            # A cache hit is a large disk read; a cancel must not wait for all of them.
            _check_cancel(cancel_event)
            block_start = block_index * block_frames
            block_end = min(scene_duration, block_start + block_frames)
            contributors = _block_contributors(visible_clips, block_start, block_end, prepared)
            fingerprint = _block_fingerprint(
                block_index=block_index,
                block_start=block_start,
                block_end=block_end,
                width=proj_w,
                height=proj_h,
                scene_fps=scene_fps,
                contributors=contributors,
            )
            block_info[block_index] = (block_start, block_end, contributors, fingerprint)
            if not contributors:
                delete_indices.add(block_index)
                count_requested(block_start, block_end)
                continue
            sources_available = all(
                prepared.get(str(getattr(clip, "source_path", "") or "")) is not None
                and bool(prepared[str(getattr(clip, "source_path", "") or "")].abs_path)
                and bool(prepared[str(getattr(clip, "source_path", "") or "")].revision)
                for clip in visible_clips
                if int(getattr(clip, "timeline_start_frame", 0) or 0) < block_end
                and int(getattr(clip, "timeline_end_frame", 0) or 0) > block_start
            )
            cached = None
            if sources_available:
                cached = load_block(
                    store,
                    block_index=block_index,
                    start=block_start,
                    end=block_end,
                    width=proj_w,
                    height=proj_h,
                    fingerprint=fingerprint,
                )
            if cached is None:
                misses.append(block_index)
                continue
            _copy_uint8_slice_to_output(
                output,
                cached,
                block_start=block_start,
                request_start=render_start,
                request_end=render_end,
            )
            count_requested(block_start, block_end)
            hit_count += 1
            hit_indices.append(block_index)

        for run in _consecutive_runs(misses):
            run_start = run[0] * block_frames
            run_end = min(scene_duration, (run[-1] + 1) * block_frames)
            status = {"failed_blocks": set(), "failures": []}
            frame_iter = iter_scene_frames(
                project,
                scene,
                run_start,
                run_end,
                cancel_event=cancel_event,
                video_capture_factory=video_capture_factory,
                prepared_sources=prepared,
                render_status=status,
                cache_block_frames=block_frames,
            )
            try:
                for block_index in run:
                    _check_cancel(cancel_event)
                    block_start, block_end, contributors, fingerprint = block_info[block_index]
                    block_array = np.empty((block_end - block_start, proj_h, proj_w, 3), dtype=np.uint8)
                    for offset in range(block_end - block_start):
                        try:
                            block_array[offset] = next(frame_iter)
                        except StopIteration as exc:
                            raise RuntimeError("Timeline renderer ended before the cache block was complete") from exc
                        count_requested(block_start + offset, block_start + offset + 1)
                    block_tensor = torch.from_numpy(block_array)
                    _copy_uint8_slice_to_output(
                        output,
                        block_tensor,
                        block_start=block_start,
                        request_start=render_start,
                        request_end=render_end,
                    )
                    if block_index in status["failed_blocks"]:
                        continue
                    try:
                        staged.append(stage_block(
                            store,
                            request_id=request_id,
                            block_index=block_index,
                            start=block_start,
                            end=block_end,
                            width=proj_w,
                            height=proj_h,
                            fingerprint=fingerprint,
                            frames=block_tensor,
                        ))
                    except Exception as exc:
                        staging_failed = True
                        logger.warning("Failed to stage render cache block %s: %s", block_index, exc)
            finally:
                close_iter = getattr(frame_iter, "close", None)
                if callable(close_iter):
                    close_iter()

        stats = {
            "hit_count": hit_count,
            "hit_indices": hit_indices,
            "miss_indices": list(misses),
            "staged_count": len(staged),
        }
        return output, prepared, staged, delete_indices, not staging_failed, stats

    staged = []
    try:
        output, prepared, staged, delete_indices, publication_ready, stats = render_attempt(staged)
        changed = _source_revisions_changed(prepared)
        if changed:
            logger.info("Render cache source revision changed; retrying request: %s", sorted(changed))
            discard_staged(staged)
            staged = []
            output, prepared, staged, delete_indices, publication_ready, stats = render_attempt(staged)
            changed = _source_revisions_changed(prepared)
            if changed:
                logger.warning(
                    "Render cache outcome=fallback scene=%s store=%s reason=source_unstable "
                    "requested_blocks=%s hit_blocks=%s miss_blocks=%s staged=%d deletions=%s sources=%s",
                    scene.scene_id,
                    store.token,
                    requested_block_indices,
                    stats["hit_indices"],
                    stats["miss_indices"],
                    stats["staged_count"],
                    sorted(delete_indices),
                    sorted(changed),
                )
                discard_staged(staged)
                return output

        if not publication_ready:
            logger.warning(
                "Render cache outcome=fallback scene=%s store=%s reason=staging_incomplete "
                "requested_blocks=%s hit_blocks=%s miss_blocks=%s staged=%d deletions=%s",
                scene.scene_id,
                store.token,
                requested_block_indices,
                stats["hit_indices"],
                stats["miss_indices"],
                stats["staged_count"],
                sorted(delete_indices),
            )
            discard_staged(staged)
            return output

        _check_cancel(cancel_event)
        published_count = len(staged)
        try:
            publish_staged(store, staged, delete_indices)
            staged = []
        except Exception as exc:
            logger.warning(
                "Render cache outcome=publication_failed scene=%s store=%s "
                "requested_blocks=%s hit_blocks=%s miss_blocks=%s staged=%d "
                "published=partial_or_unknown deletions=%s reason=%s",
                scene.scene_id,
                store.token,
                requested_block_indices,
                stats["hit_indices"],
                stats["miss_indices"],
                stats["staged_count"],
                sorted(delete_indices),
                exc,
            )
        else:
            retention = None
            should_enforce_retention = published_count > 0 or bool(delete_indices)
            if should_enforce_retention:
                try:
                    retention = enforce_render_cache_budget(
                        project,
                        cache_max_bytes,
                        protected_tokens={store.token},
                    )
                except Exception as exc:
                    logger.warning(
                        "Render cache retention_failed scene=%s store=%s budget=%s reason=%s",
                        scene.scene_id,
                        store.token,
                        "unlimited" if cache_max_bytes is None else cache_max_bytes,
                        exc,
                    )
            if retention is not None:
                logger.info(
                    "Render cache retention_ready scene=%s store=%s budget=%s "
                    "entries=%d size_bytes=%d deleted=%s deleted_bytes=%d protected=%s "
                    "over_budget_bytes=%d pending=%s",
                    scene.scene_id,
                    store.token,
                    "unlimited" if cache_max_bytes is None else cache_max_bytes,
                    retention["entry_count"],
                    retention["size_bytes"],
                    retention["deleted"],
                    retention["deleted_bytes"],
                    retention["protected"],
                    retention["over_budget_bytes"],
                    retention["pending"],
                )
            logger.info(
                "Render cache outcome=ready scene=%s root=%s store=%s block_frames=%d "
                "requested_blocks=%s hit_blocks=%s miss_blocks=%s staged=%d published=%d deletions=%s",
                scene.scene_id,
                store.root,
                store.token,
                block_frames,
                requested_block_indices,
                stats["hit_indices"],
                stats["miss_indices"],
                stats["staged_count"],
                published_count,
                sorted(delete_indices),
            )
        return output
    except TimelineRenderCancelled:
        discard_staged(staged)
        raise
    except Exception:
        discard_staged(staged)
        raise
    finally:
        discard_staged(staged)
        store_activity.__exit__(None, None, None)


def _audio_contributors(project, scene, start_frame, end_frame):
    from .audio_pipeline import scene_audio_spec
    return scene_audio_spec(project, scene, start_frame, end_frame)[2]



def mix_scene_audio_to_wav(project, scene, start_frame, end_frame, output_wav, *, cancel_event=None):
    """Shared float renderer; callers retain cancellation and persistence ownership."""
    from .audio_pipeline import mix_scene_audio_to_wav as render_audio
    return render_audio(project, scene, start_frame, end_frame, output_wav, cancel_event=cancel_event)
