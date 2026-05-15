from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import cv2

from .media_helpers import (
    CUSTOM_OUTPUT_KIND_VIDEO,
    CUSTOM_SAVE_VIDEO_PRESET,
    MediaOperationCancelled,
    audio_only_export_spec,
    encode_audio,
    encode_video,
    get_ffmpeg_path,
    metadata_for_save_preset,
    normalize_save_preset,
    output_extension_for_custom_options,
    output_extension_for_preset,
    resolve_custom_export_options,
    run_ffmpeg_command,
    save_video_encode_timeout_seconds,
    tensor_mode_for_preset,
    tensor_to_uint8_frames,
)
from .project_manager import load_project, save_project
from .thumbnail_service import ensure_thumbnail
from .timeline_renderer import (
    TimelineRenderCancelled,
    mix_scene_audio_to_wav,
    render_scene_frames,
)
from .timeline_state import Asset, AudioTrack, ClipReference, LaneConfig, Scene, TimelineProject

logger = logging.getLogger("sonder_editor")


class ExportAlreadyRunning(RuntimeError):
    def __init__(self, job_id: str):
        super().__init__("export_running")
        self.job_id = job_id


@dataclass
class TimelineExportJob:
    job_id: str
    project_key: str
    project_id: str
    project_dir: str
    request: dict
    status: str = "running"
    phase: str = "queued"
    message: str = "Queued..."
    code: str = ""
    error: str = ""
    result_asset_id: str = ""
    result_scene_id: str = ""
    placed_clip: dict | None = None
    warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: concurrent.futures.Future | None = None

    def public_status(self) -> dict:
        payload = {
            "job_id": self.job_id,
            "status": self.status,
            "phase": self.phase,
        }
        if self.message:
            payload["message"] = self.message
        if self.status == "failed":
            payload["code"] = self.code or "export_failed"
            payload["error"] = self.error or "Export failed"
        if self.status == "cancelled":
            payload["cancelled"] = True
        return payload


def _project_key(project_dir: str) -> str:
    return os.path.normcase(os.path.abspath(project_dir))


def _coerce_bool(value, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_filename_component(value: str, fallback: str) -> str:
    component = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "").strip())
    component = re.sub(r"-+", "-", component).strip("-_")
    return component[:80] or fallback


def _scene_slug(scene: Scene) -> str:
    return _safe_filename_component(getattr(scene, "name", "") or "scene", "scene").lower()


def _normalize_asset_folder(folder: str) -> str:
    return str(folder or "").strip().replace("\\", "/").strip("/")


def _safe_folder_components(folder: str) -> list[str]:
    normalized = _normalize_asset_folder(folder)
    if not normalized:
        return []
    components = []
    for part in normalized.split("/"):
        safe = _safe_filename_component(part, "folder")
        if safe:
            components.append(safe)
    return components


def _ensure_asset_folder(project: TimelineProject, folder: str) -> None:
    normalized = _normalize_asset_folder(folder)
    if not normalized:
        return
    existing = {
        _normalize_asset_folder(entry)
        for entry in project.metadata.get("asset_folders", [])
        if _normalize_asset_folder(entry)
    }
    if normalized not in existing:
        existing.add(normalized)
        project.metadata["asset_folders"] = sorted(existing)


def _effective_scene_fps(project: TimelineProject, scene: Scene) -> float:
    return max(0.001, float(getattr(scene, "fps", 0.0) or project.fps or 24.0))


def _scene_resolution(project: TimelineProject, scene: Scene) -> tuple[int, int]:
    width, height = project.resolution
    if getattr(scene, "width", 0) > 0:
        width = int(scene.width)
    if getattr(scene, "height", 0) > 0:
        height = int(scene.height)
    return max(1, int(width or 1)), max(1, int(height or 1))


def _media_output_dir(project_dir: str, folder: str) -> str:
    media_dir = os.path.join(project_dir, "media")
    components = _safe_folder_components(folder)
    output_dir = os.path.join(media_dir, *components) if components else media_dir
    os.makedirs(output_dir, exist_ok=True)
    return output_dir


def _output_path(project_dir: str, scene: Scene, prefix: str, extension: str, folder: str) -> str:
    output_dir = _media_output_dir(project_dir, folder)
    safe_prefix = _safe_filename_component(prefix, "export")
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{safe_prefix}_{_scene_slug(scene)}_{timestamp}_{uuid.uuid4().hex[:6]}{extension}"
    return os.path.join(output_dir, filename)


def _rel_media_path(project_dir: str, output_path: str) -> str:
    return os.path.relpath(output_path, project_dir).replace("\\", "/")


def _hidden_lane_indexes(configs: list) -> set[int]:
    return {
        idx
        for idx, cfg in enumerate(configs or [])
        if getattr(cfg, "hidden", False)
    }


def _video_sources(project: TimelineProject, scene: Scene, start: int, end: int) -> list[dict]:
    hidden = _hidden_lane_indexes(getattr(scene, "video_lane_configs", []))
    sources = []
    for clip in getattr(scene, "clips", []) or []:
        if getattr(clip, "track_index", 0) in hidden:
            continue
        if getattr(clip, "role", "render") != "render" or getattr(clip, "muted", False):
            continue
        overlap_start = max(start, int(clip.timeline_start_frame or 0))
        overlap_end = min(end, int(clip.timeline_end_frame or 0))
        if overlap_end <= overlap_start:
            continue
        asset = next((item for item in project.assets if item.path == clip.source_path), None)
        sources.append({
            "asset_id": getattr(asset, "asset_id", "") if asset else "",
            "track_index": int(getattr(clip, "track_index", 0) or 0),
            "source_in": int(getattr(clip, "source_in_frame", 0) or 0) + (overlap_start - int(clip.timeline_start_frame or 0)),
            "source_out": int(getattr(clip, "source_in_frame", 0) or 0) + (overlap_end - int(clip.timeline_start_frame or 0)),
            "timeline_start": overlap_start,
            "timeline_end": overlap_end,
        })
    return sources


def _audio_sources(project: TimelineProject, scene: Scene, start: int, end: int) -> list[dict]:
    hidden = _hidden_lane_indexes(getattr(scene, "audio_lane_configs", []))
    sources = []
    for track in getattr(scene, "audio_tracks", []) or []:
        if getattr(track, "lane_index", 0) in hidden or getattr(track, "muted", False):
            continue
        overlap_start = max(start, int(track.timeline_start_frame or 0))
        overlap_end = min(end, int(track.timeline_end_frame or 0))
        if overlap_end <= overlap_start:
            continue
        source_path = str(getattr(track, "source_path", "") or "")
        abs_path = source_path if os.path.isfile(source_path) else os.path.join(project.project_dir, source_path)
        if not os.path.isfile(abs_path):
            continue
        asset = next((item for item in project.assets if item.path == source_path), None)
        source_in = int(getattr(track, "source_in_frame", 0) or 0) + (overlap_start - int(track.timeline_start_frame or 0))
        sources.append({
            "asset_id": getattr(asset, "asset_id", "") if asset else "",
            "lane_index": int(getattr(track, "lane_index", 0) or 0),
            "source_in": source_in,
            "source_out": source_in + (overlap_end - overlap_start),
            "timeline_start": overlap_start,
            "timeline_end": overlap_end,
            "volume": float(getattr(track, "volume", 1.0) or 1.0),
        })
    return sources


def _editor_export_metadata(
    project: TimelineProject,
    scene: Scene,
    start: int,
    end: int,
    *,
    preset_id: str,
    custom_options: dict | None,
    include_video: bool,
    include_audio: bool,
) -> dict:
    width, height = _scene_resolution(project, scene)
    return {
        "schema_version": "1.0",
        "produced_by": {"tool": "sonder-editor", "version": "", "comfyui_version": ""},
        "exported_at": datetime.now().isoformat(),
        "project_name": project.name,
        "scene_id": scene.scene_id,
        "scene_name": scene.name,
        "range": {"start": start, "end": end},
        "fps": _effective_scene_fps(project, scene),
        "resolution": {"width": width, "height": height},
        "preset": preset_id,
        "custom_encode": dict(custom_options) if custom_options else None,
        "include_video": include_video,
        "include_audio": include_audio,
        "sources": _video_sources(project, scene, start, end),
        "audio_sources": _audio_sources(project, scene, start, end),
    }


def _technical_video_metadata(path: str, fallback: dict) -> dict:
    cap = None
    try:
        cap = cv2.VideoCapture(path)
        if cap.isOpened():
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or fallback.get("width", 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or fallback.get("height", 0)
            frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or fallback.get("frame_count", 0)
            fps = float(cap.get(cv2.CAP_PROP_FPS) or fallback.get("fps", 0.0))
            return {
                "width": width,
                "height": height,
                "frame_count": frame_count,
                "fps": fps,
                "duration_sec": frame_count / fps if fps > 0 else fallback.get("duration_sec", 0.0),
            }
    except Exception as exc:
        logger.warning("Export video metadata probe failed for %s: %s", path, exc)
    finally:
        if cap is not None:
            cap.release()
    return dict(fallback)


def _place_video_take(
    project: TimelineProject,
    scene: Scene,
    asset: Asset,
    start: int,
    end: int,
) -> ClipReference:
    total_frames = max(1, int(asset.frame_count or (end - start) or 1))
    existing_lanes = [int(getattr(clip, "track_index", 0) or 0) for clip in getattr(scene, "clips", [])]
    new_lane = (max(existing_lanes) if existing_lanes else -1) + 1
    if scene.video_lane_count <= new_lane:
        scene.video_lane_count = new_lane + 1
    while len(scene.video_lane_configs) < scene.video_lane_count:
        scene.video_lane_configs.append(LaneConfig())

    clip = ClipReference(
        source_path=asset.path,
        source_in_frame=0,
        source_out_frame=total_frames,
        total_source_frames=total_frames,
        source_origin_frame=0,
        timeline_start_frame=start,
        timeline_end_frame=end,
        track_index=new_lane,
        is_generated=True,
        generation_params=dict(asset.generation_params),
        take_metadata=dict(asset.generation_params),
    )
    scene.clips.append(clip)
    return clip


def _place_embedded_audio_take(
    project: TimelineProject,
    scene: Scene,
    video_asset: Asset,
    start: int,
    end: int,
    folder: str,
    generation_params: dict,
    *,
    cancel_event=None,
    cleanup_paths: list[str] | None = None,
) -> tuple[AudioTrack, str] | None:
    if not getattr(video_asset, "has_audio", False):
        return None

    video_path = os.path.join(project.project_dir, video_asset.path)
    if not os.path.isfile(video_path):
        return None

    audio_filename = f"{video_asset.asset_id}_audio.wav"
    audio_abs_path = os.path.join(_media_output_dir(project.project_dir, folder), audio_filename)
    audio_rel_path = _rel_media_path(project.project_dir, audio_abs_path)
    if cleanup_paths is not None:
        cleanup_paths.append(audio_abs_path)

    cmd = [
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        str(audio_abs_path),
    ]
    run_ffmpeg_command(
        cmd,
        timeout=max(30, int(getattr(video_asset, "duration_sec", 0.0) or 0) + 60),
        cancel_event=cancel_event,
    )
    if not os.path.isfile(audio_abs_path) or os.path.getsize(audio_abs_path) <= 1024:
        try:
            if os.path.isfile(audio_abs_path):
                os.remove(audio_abs_path)
        except OSError:
            pass
        if cleanup_paths is not None and audio_abs_path in cleanup_paths:
            cleanup_paths.remove(audio_abs_path)
        return None

    fps = max(0.001, float(getattr(video_asset, "fps", 0.0) or project.fps or 24.0))
    total_frames = max(1, end - start)
    audio_asset = _register_export_asset(
        project,
        audio_abs_path,
        asset_type="audio",
        folder=folder,
        technical_metadata={
            "duration_sec": total_frames / fps,
            "sample_rate": 44100,
        },
        generation_params={
            **dict(generation_params),
            "source_video_asset_id": video_asset.asset_id,
            "extracted_for_take_audio": True,
        },
    )

    existing_lanes = [int(getattr(track, "lane_index", 0) or 0) for track in getattr(scene, "audio_tracks", [])]
    new_lane = (max(existing_lanes) if existing_lanes else -1) + 1
    if scene.audio_lane_count <= new_lane:
        scene.audio_lane_count = new_lane + 1
    while len(scene.audio_lane_configs) < scene.audio_lane_count:
        scene.audio_lane_configs.append(LaneConfig())

    track = AudioTrack(
        source_path=audio_asset.path,
        timeline_start_frame=start,
        timeline_end_frame=end,
        source_in_frame=0,
        total_source_frames=total_frames,
        source_origin_frame=0,
        lane_index=new_lane,
    )
    scene.audio_tracks.append(track)
    return track, audio_abs_path


def _register_export_asset(
    project: TimelineProject,
    output_path: str,
    *,
    asset_type: str,
    folder: str,
    technical_metadata: dict,
    generation_params: dict,
) -> Asset:
    filename = os.path.basename(output_path)
    rel_path = _rel_media_path(project.project_dir, output_path)
    normalized_rel_path = rel_path.replace("\\", "/")
    asset = next(
        (
            existing
            for existing in getattr(project, "assets", []) or []
            if str(getattr(existing, "path", "") or "").replace("\\", "/") == normalized_rel_path
        ),
        None,
    )
    if asset is None:
        asset = Asset(name=filename, asset_type=asset_type, path=rel_path)
        project.add_asset(asset)

    asset.name = asset.name or filename
    asset.asset_type = asset_type
    asset.path = rel_path
    asset.width = int(technical_metadata.get("width", 0) or 0)
    asset.height = int(technical_metadata.get("height", 0) or 0)
    asset.frame_count = int(technical_metadata.get("frame_count", 0) or 0)
    asset.fps = float(technical_metadata.get("fps", 0.0) or 0.0)
    asset.duration_sec = float(technical_metadata.get("duration_sec", 0.0) or 0.0)
    asset.sample_rate = int(technical_metadata.get("sample_rate", 0) or 0)
    asset.has_audio = bool(technical_metadata.get("has_audio", False))
    asset.folder = _normalize_asset_folder(folder)
    asset.generation_params = dict(generation_params)
    if asset.folder:
        _ensure_asset_folder(project, asset.folder)
    project.modified_at = datetime.now().isoformat()
    try:
        thumb_path = os.path.join(project.project_dir, "cache", "thumbnails", f"{asset.asset_id}.png")
        ensure_thumbnail(asset.asset_type, output_path, thumb_path)
    except Exception as exc:
        logger.warning("Failed to generate export thumbnail for %s: %s", output_path, exc)
    return asset


class TimelineExportManager:
    def __init__(self, *, max_workers: int = 2, ttl_seconds: int = 10 * 60):
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sonder-export")
        self._lock = threading.Lock()
        self._jobs: dict[str, TimelineExportJob] = {}
        self._active_by_project: dict[str, str] = {}
        self._ttl_seconds = ttl_seconds

    def start(self, project: TimelineProject, request_body: dict[str, Any]) -> TimelineExportJob:
        self._cleanup()
        project_key = _project_key(project.project_dir)
        with self._lock:
            active_id = self._active_by_project.get(project_key)
            active_job = self._jobs.get(active_id or "")
            if active_job and active_job.status == "running":
                raise ExportAlreadyRunning(active_job.job_id)

            job = TimelineExportJob(
                job_id=uuid.uuid4().hex[:12],
                project_key=project_key,
                project_id=project.project_id,
                project_dir=project.project_dir,
                request=self._normalize_request(project, request_body),
            )
            self._jobs[job.job_id] = job
            self._active_by_project[project_key] = job.job_id
            job.future = self._executor.submit(self._run_job, job)
            return job

    def get(self, job_id: str) -> TimelineExportJob | None:
        self._cleanup()
        with self._lock:
            return self._jobs.get(str(job_id or ""))

    def cancel(self, job_id: str) -> TimelineExportJob | None:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if job and job.status == "running":
                job.cancel_event.set()
                job.phase = "cancelling"
                job.message = "Cancelling..."
            return job

    def _cleanup(self) -> None:
        cutoff = time.time() - self._ttl_seconds
        with self._lock:
            for job_id, job in list(self._jobs.items()):
                if job.status == "running":
                    continue
                if job.completed_at and job.completed_at < cutoff:
                    self._jobs.pop(job_id, None)

    def _normalize_request(self, project: TimelineProject, body: dict[str, Any]) -> dict:
        scene_id = str(body.get("scene_id") or "")
        scene = project.get_scene(scene_id)
        if not scene:
            raise ValueError(f"Scene not found: {scene_id}")

        raw_range = body.get("range") if isinstance(body.get("range"), dict) else {}
        scene_duration = max(0, int(scene.duration_frames or 0))
        start = max(0, min(scene_duration, _coerce_int(raw_range.get("start"), 0)))
        end = max(start, min(scene_duration, _coerce_int(raw_range.get("end"), scene_duration)))
        if end <= start:
            raise ValueError("Export range must contain at least one frame")

        preset_id = normalize_save_preset(body.get("save_preset"))
        custom_options = body.get("custom_options") if isinstance(body.get("custom_options"), dict) else None
        if preset_id == CUSTOM_SAVE_VIDEO_PRESET:
            custom_options = dict(custom_options or {})
            custom_options["custom_output_kind"] = CUSTOM_OUTPUT_KIND_VIDEO
            resolve_custom_export_options(custom_options)
        else:
            custom_options = None

        include_video = _coerce_bool(body.get("include_video"), True)
        include_audio = _coerce_bool(body.get("include_audio"), True)
        if not include_video and not include_audio:
            raise ValueError("Enable video or audio to export")
        if not include_video and not getattr(scene, "audio_tracks", []):
            raise ValueError("Audio-only export requires at least one scene audio track")

        return {
            "scene_id": scene_id,
            "range": {"start": start, "end": end},
            "filename_prefix": _safe_filename_component(str(body.get("filename_prefix") or ""), "export"),
            "save_preset": preset_id,
            "custom_options": custom_options,
            "include_video": include_video,
            "include_audio": include_audio,
            "place_as_take": include_video and _coerce_bool(body.get("place_as_take"), True),
        }

    def _set_phase(self, job: TimelineExportJob, phase: str, message: str) -> None:
        job.phase = phase
        job.message = message

    def _check_cancel(self, job: TimelineExportJob) -> None:
        if job.cancel_event.is_set():
            raise TimelineRenderCancelled("timeline export cancelled")

    def _run_job(self, job: TimelineExportJob) -> None:
        cleanup_paths: list[str] = []
        final_output_path = ""
        try:
            self._check_cancel(job)
            project = load_project(job.project_dir)
            scene = project.get_scene(job.request["scene_id"])
            if not scene:
                raise ValueError(f"Scene not found: {job.request['scene_id']}")
            start = int(job.request["range"]["start"])
            end = int(job.request["range"]["end"])
            fps = _effective_scene_fps(project, scene)
            preset_id = job.request["save_preset"]
            custom_options = job.request["custom_options"]
            include_video = bool(job.request["include_video"])
            include_audio = bool(job.request["include_audio"])
            place_as_take = bool(job.request["place_as_take"])
            width, height = _scene_resolution(project, scene)
            frame_count = end - start

            editor_export = _editor_export_metadata(
                project,
                scene,
                start,
                end,
                preset_id=preset_id,
                custom_options=custom_options,
                include_video=include_video,
                include_audio=include_audio,
            )

            rgb_frames = None
            if include_video:
                self._set_phase(job, "compositing", "Compositing frames...")
                tensor = render_scene_frames(project, scene, start, end, cancel_event=job.cancel_event)
                tensor_mode = "round"
                if preset_id == CUSTOM_SAVE_VIDEO_PRESET:
                    tensor_mode = resolve_custom_export_options(custom_options)["tensor_mode"]
                else:
                    tensor_mode = tensor_mode_for_preset(preset_id)
                rgb_frames = tensor_to_uint8_frames(tensor, mode=tensor_mode)
                frame_count = int(rgb_frames.shape[0])
                height, width = rgb_frames.shape[1], rgb_frames.shape[2]

            mixed_audio_path = None
            mixed_audio_contributors = []
            if include_audio and getattr(scene, "audio_tracks", []):
                self._set_phase(job, "mixing_audio", "Mixing audio...")
                mixed_audio_path = os.path.join(project.project_dir, "media", f"_tmp_export_audio_{job.job_id}.wav")
                cleanup_paths.append(mixed_audio_path)
                mixed_audio_contributors = mix_scene_audio_to_wav(
                    project,
                    scene,
                    start,
                    end,
                    mixed_audio_path,
                    cancel_event=job.cancel_event,
                )
            elif include_audio:
                job.warnings.append("Scene has no audio tracks; exported video has no audio stream.")

            self._check_cancel(job)
            self._set_phase(job, "encoding", "Encoding...")
            folder = "" if place_as_take and include_video else "Exports"
            if include_video:
                extension = output_extension_for_custom_options(custom_options) if preset_id == CUSTOM_SAVE_VIDEO_PRESET else output_extension_for_preset(preset_id)
            else:
                audio_spec = audio_only_export_spec(preset_id, custom_options)
                extension = audio_spec["extension"]
            final_output_path = _output_path(project.project_dir, scene, job.request["filename_prefix"], extension, folder)
            temp_output_path = f"{os.path.splitext(final_output_path)[0]}.{job.job_id}.tmp{extension}"
            cleanup_paths.append(temp_output_path)
            cleanup_paths.append(final_output_path)

            if include_video:
                encode_timeout = save_video_encode_timeout_seconds(
                    preset_id,
                    frame_count,
                    width,
                    height,
                    custom_options,
                )
                encode_metadata = encode_video(
                    rgb_frames,
                    preset_id=preset_id,
                    output_path=temp_output_path,
                    fps=fps,
                    audio_path=mixed_audio_path if mixed_audio_path and os.path.isfile(mixed_audio_path) else None,
                    custom_options=custom_options,
                    timeout=encode_timeout,
                    cancel_event=job.cancel_event,
                )
            else:
                if not mixed_audio_path or not os.path.isfile(mixed_audio_path):
                    raise ValueError("Audio-only export had no mixed audio source")
                audio_spec = audio_only_export_spec(preset_id, custom_options)
                encode_audio(
                    mixed_audio_path,
                    temp_output_path,
                    codec=audio_spec["codec"],
                    container=audio_spec["container"],
                    bitrate_kbps=audio_spec.get("bitrate_kbps"),
                    cancel_event=job.cancel_event,
                )
                encode_metadata = dict(audio_spec["metadata"])

            self._check_cancel(job)
            os.replace(temp_output_path, final_output_path)
            cleanup_paths.remove(temp_output_path)
            self._check_cancel(job)

            self._set_phase(job, "registering", "Registering asset...")
            current_project = load_project(job.project_dir)
            current_scene = current_project.get_scene(job.request["scene_id"])
            if place_as_take and include_video and not current_scene:
                job.warnings.append("Target scene was deleted; placement skipped.")

            generation_params = dict(encode_metadata)
            generation_params["editor_export"] = editor_export

            if include_video:
                technical = _technical_video_metadata(final_output_path, {
                    "width": width,
                    "height": height,
                    "frame_count": frame_count,
                    "fps": fps,
                    "duration_sec": frame_count / fps if fps > 0 else 0.0,
                })
                technical["has_audio"] = bool(mixed_audio_path and os.path.isfile(mixed_audio_path) and mixed_audio_contributors is not None)
                asset_type = "video"
            else:
                technical = {
                    "duration_sec": (end - start) / fps if fps > 0 else 0.0,
                    "sample_rate": 44100,
                }
                asset_type = "audio"

            asset = _register_export_asset(
                current_project,
                final_output_path,
                asset_type=asset_type,
                folder=folder,
                technical_metadata=technical,
                generation_params=generation_params,
            )
            self._check_cancel(job)

            placed_audio_cleanup_path = ""
            if place_as_take and include_video and current_scene:
                self._set_phase(job, "placing_take", "Placing take...")
                clip = _place_video_take(current_project, current_scene, asset, start, end)
                job.placed_clip = clip.to_dict()
                job.result_scene_id = current_scene.scene_id
                if asset.has_audio:
                    try:
                        audio_take = _place_embedded_audio_take(
                            current_project,
                            current_scene,
                            asset,
                            start,
                            end,
                            folder,
                            generation_params,
                            cancel_event=job.cancel_event,
                            cleanup_paths=cleanup_paths,
                        )
                        if audio_take:
                            _audio_track, placed_audio_cleanup_path = audio_take
                        else:
                            job.warnings.append("Placed video take, but embedded audio extraction did not produce a timeline audio track.")
                    except (TimelineRenderCancelled, MediaOperationCancelled):
                        raise
                    except Exception as exc:
                        logger.warning("Timeline export take audio extraction failed: %s", exc)
                        job.warnings.append("Placed video take, but embedded audio extraction failed.")

            save_project(current_project)
            cleanup_paths.remove(final_output_path)
            if placed_audio_cleanup_path and placed_audio_cleanup_path in cleanup_paths:
                cleanup_paths.remove(placed_audio_cleanup_path)
            job.result_asset_id = asset.asset_id
            job.status = "completed"
            job.phase = "done"
            job.message = "Done"
        except (TimelineRenderCancelled, MediaOperationCancelled):
            job.status = "cancelled"
            job.phase = "cancelled"
            job.message = "Cancelled"
            for path in cleanup_paths:
                try:
                    if path and os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
        except Exception as exc:
            logger.exception("Timeline export failed")
            job.status = "failed"
            job.phase = "failed"
            job.code = "export_failed"
            job.error = str(exc)
            job.message = str(exc)
            for path in cleanup_paths:
                try:
                    if path and os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
        finally:
            if job.status == "running":
                job.status = "failed"
                job.phase = "failed"
                job.error = "Export ended without a terminal status"
                job.code = "export_failed"
            job.completed_at = time.time()
            with self._lock:
                if self._active_by_project.get(job.project_key) == job.job_id:
                    self._active_by_project.pop(job.project_key, None)
            for path in list(cleanup_paths):
                try:
                    if path and os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
