import asyncio
import json
import logging
import math
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from aiohttp import web

from .media_helpers import (
    CROP_POSITIONS,
    DEFAULT_CROP_POSITION,
    DEFAULT_FIT_MODE,
    FIT_MODES,
    MediaProbeError,
    apply_rgb_color_correction,
    color_correction_for_interpretation,
    decode_video_frame,
    get_ffmpeg_path,
    get_ffprobe_path,
    is_valid_media_metadata,
    resolve_source_color_interpretation,
    probe_audio_duration,
    probe_media_has_audio,
    probe_media_metadata,
    probe_video_color_metadata,
    resize_frame_to_long_edge,
    write_png,
)
from .path_security import (
    PathSecurityError,
    normalize_project_relative_path,
    log_path_quarantine,
    path_within as _security_path_within,
    project_media_path,
    project_media_root,
    resolve_comfy_input_path,
    resolve_existing_project_path,
    resolve_project_path,
    resolve_static_path,
    safe_route_token as _security_safe_route_token,
    sanitize_filename_component,
)
from .atomic_io import atomic_replace
from .upload_streaming import (
    UPLOAD_STAGING_DIRNAME,
    UploadRequestError,
    active_upload_count,
    ensure_upload_disk_space,
    receive_project_upload,
)
from .project_manager import (
    ProjectVersionConflict,
    create_project,
    load_project,
    save_project,
    list_projects,
    register_project_saved_hook,
)
from .session_registry import (
    claim_session,
    create_handoff,
    get_canvas_host,
    get_diag_state,
    get_project_debug_state,
    get_widget_state,
    get_owner,
    heartbeat_session,
    record_diag_event,
    register_canvas_host,
    release_session,
    remember_event_loop,
    refresh_canvas_host,
    schedule_project_event,
    seed_widget_state,
    subscribe,
    unregister_canvas_host,
    unsubscribe,
    update_widget_state,
)
from .timeline_state import (
    TimelineProject, Asset, Scene, GuideFrame, PromptSection, AudioTrack,
    ClipReference, LaneConfig, GenerationJob, apply_color_metadata, classify_asset_path,
    effective_scene_fps, media_timeline_frames, retime_scene_geometry,
)
from .thumbnail_service import ensure_thumbnail, generate_thumbnail_strip, generate_waveform_data
from .timeline_export import ExportAlreadyRunning, TimelineExportManager
from . import external_links
from . import prompt_payload
from .guide_collision import resolve_execution_window, resolve_guide_collisions

logger = logging.getLogger("sonder_editor")
_TIMELINE_EXPORTS = TimelineExportManager()
_ASSET_DERIVED_CACHE_HEADERS = {
    "Cache-Control": "public, max-age=0, must-revalidate",
}
_BAD_PROJECT_REQUEST_PREFIX = "__sonder_bad_project_request__:"
_SONDER_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_SONDER_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    "X-Frame-Options": "SAMEORIGIN",
}
_SONDER_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' ws: wss:; "
    "object-src 'none'; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'self'"
)

# Defer route registration until ComfyUI's PromptServer is available.
try:
    from server import PromptServer
    routes = PromptServer.instance.routes
except Exception:
    routes = None
    logger.warning("PromptServer not available — Sonder Editor API routes disabled")


def _json_error(msg: str, status: int = 400) -> web.Response:
    msg = str(msg)
    if status == 404 and msg.startswith(_BAD_PROJECT_REQUEST_PREFIX):
        msg = msg[len(_BAD_PROJECT_REQUEST_PREFIX):]
        status = 400
    return web.json_response({"error": msg}, status=status)


def _attach_project_version_headers(
    response: web.StreamResponse,
    project_id: str = "",
    modified_at: str = "",
) -> web.StreamResponse:
    project_id = str(project_id or "")
    modified_at = str(modified_at or "")
    if project_id:
        response.headers["X-Sonder-Project-Id"] = project_id
    if modified_at:
        response.headers["X-Sonder-Project-Modified-At"] = modified_at
    return response


def _request_host_candidates(request: web.Request) -> set[str]:
    candidates = set()
    headers = getattr(request, "headers", {}) or {}
    for value in (
        getattr(request, "host", ""),
        headers.get("Host", ""),
        headers.get("X-Forwarded-Host", ""),
    ):
        first = str(value or "").split(",", 1)[0].strip().lower()
        if first:
            candidates.add(first)
    return candidates


def _same_origin_request(request: web.Request) -> bool:
    origin = str((getattr(request, "headers", {}) or {}).get("Origin", "") or "").strip()
    if not origin:
        return True
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    return parsed.netloc.lower() in _request_host_candidates(request)


def _apply_sonder_security_headers(request: web.Request, response: web.StreamResponse) -> web.StreamResponse:
    path = str(getattr(request, "path", "") or "")
    if not path.startswith("/sonder-editor/"):
        return response
    for key, value in _SONDER_SECURITY_HEADERS.items():
        response.headers.setdefault(key, value)
    response.headers.setdefault("Content-Security-Policy", _SONDER_CSP)
    return response


def _cross_origin_blocked_response(request: web.Request) -> web.StreamResponse:
    response = web.json_response(
        {"error": "Cross-origin Sonder Editor request blocked", "code": "cross_origin_blocked"},
        status=403,
    )
    return _apply_sonder_security_headers(request, response)


@web.middleware
async def _sonder_security_middleware(request: web.Request, handler):
    path = str(getattr(request, "path", "") or "")
    method = str(getattr(request, "method", "") or "").upper()
    if path.startswith("/sonder-editor/") and method in _SONDER_MUTATING_METHODS and not _same_origin_request(request):
        return _cross_origin_blocked_response(request)
    response = await handler(request)
    return _apply_sonder_security_headers(request, response)


def _remember_request_project(request: web.Request, project: TimelineProject) -> None:
    try:
        request["sonder_editor_project"] = project
    except Exception:
        try:
            setattr(request, "_sonder_editor_project", project)
        except Exception:
            pass


def _request_project(request: web.Request) -> TimelineProject | None:
    try:
        project = request.get("sonder_editor_project")
    except Exception:
        project = None
    if project is None:
        project = getattr(request, "_sonder_editor_project", None)
    return project if isinstance(project, TimelineProject) else None


def _project_saved_event(project: TimelineProject) -> None:
    canonical_project_id = str(getattr(project, "project_id", "") or "")
    project_dir = str(getattr(project, "project_dir", "") or "")
    folder_project_id = os.path.basename(os.path.normpath(project_dir)) if project_dir else ""

    aliases = []
    seen = set()
    for alias in (canonical_project_id, folder_project_id):
        if not alias or alias in seen:
            continue
        aliases.append(alias)
        seen.add(alias)

    for alias in aliases:
        schedule_project_event(
            alias,
            {
                "type": "project_updated",
                "project_id": alias,
                "canonical_project_id": canonical_project_id,
                "modified_at": getattr(project, "modified_at", ""),
            },
        )


register_project_saved_hook(_project_saved_event)


@web.middleware
async def _project_conflict_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except ProjectVersionConflict as exc:
        # #36 / 409-race diagnostic: capture every project_version_conflict so the
        # diag ring shows when stale `If-Match` headers collide with concurrent writers.
        # Lets us correlate snap-back symptoms with the request path that 409'd.
        try:
            project_id = ""
            try:
                project_id = str(request.match_info.get("project_id", "") or "")
            except Exception:
                project_id = ""
            record_diag_event(
                "project_version_conflict_409",
                project_id=project_id,
                path=str(request.path or ""),
                method=str(request.method or ""),
                expected_modified_at=str(exc.expected_modified_at or ""),
                actual_modified_at=str(exc.actual_modified_at or ""),
            )
        except Exception:
            logger.debug("project_conflict_middleware failed to emit diag event", exc_info=True)
        payload = {
            "error": "project_version_conflict",
            "code": "project_version_conflict",
            "expected_modified_at": exc.expected_modified_at,
            "actual_modified_at": exc.actual_modified_at,
            "project": exc.current_data,
        }
        response = web.json_response(payload, status=409)
        current = exc.current_data or {}
        _attach_project_version_headers(
            response,
            current.get("project_id", "") or project_id,
            exc.actual_modified_at or current.get("modified_at", ""),
        )
        return response


try:
    app = PromptServer.instance.app if routes is not None else None
    if app is not None and not getattr(app, "_sonder_project_conflict_middleware", False):
        app.middlewares.append(_project_conflict_middleware)
        setattr(app, "_sonder_project_conflict_middleware", True)
except Exception:
    logger.debug("Could not install Sonder project conflict middleware", exc_info=True)


@web.middleware
async def _project_version_header_middleware(request: web.Request, handler):
    response = await handler(request)
    try:
        path = str(request.path or "")
        status = int(getattr(response, "status", 0) or 0)
        if path.startswith("/sonder-editor/project/") and 200 <= status < 400:
            project = _request_project(request)
            if project is not None:
                _attach_project_version_headers(
                    response,
                    getattr(project, "project_id", "") or request.match_info.get("project_id", ""),
                    getattr(project, "modified_at", ""),
                )
    except Exception:
        logger.debug("Could not attach Sonder project version headers", exc_info=True)
    return response


try:
    if app is not None and not getattr(app, "_sonder_project_version_header_middleware", False):
        app.middlewares.append(_project_version_header_middleware)
        setattr(app, "_sonder_project_version_header_middleware", True)
except Exception:
    logger.debug("Could not install Sonder project version header middleware", exc_info=True)


try:
    if app is not None and not getattr(app, "_sonder_security_middleware", False):
        app.middlewares.append(_sonder_security_middleware)
        setattr(app, "_sonder_security_middleware", True)
except Exception:
    logger.debug("Could not install Sonder security middleware", exc_info=True)


# Route-timing diagnostic. When `SONDER_DEBUG_SESSION` is enabled, logs any
# `/sonder-editor/*` handler that took longer than the threshold to complete.
# Lets us correlate disconnects with the specific handler that blocked the
# Python event loop. Zero-allocation when the flag is off — `record_diag_event`
# short-circuits before any payload is built.
_ROUTE_TIMING_THRESHOLD_S = 0.5


@web.middleware
async def _route_timing_middleware(request: web.Request, handler):
    started = time.monotonic()
    response = None
    try:
        response = await handler(request)
        return response
    finally:
        elapsed = time.monotonic() - started
        if elapsed >= _ROUTE_TIMING_THRESHOLD_S:
            try:
                path = request.path or ""
                if path.startswith("/sonder-editor/"):
                    project_id = ""
                    try:
                        project_id = str(request.match_info.get("project_id", "") or "")
                    except Exception:
                        project_id = ""
                    record_diag_event(
                        "route_blocking",
                        project_id=project_id,
                        path=path,
                        method=request.method,
                        duration_ms=elapsed * 1000.0,
                        status=int(getattr(response, "status", 0) or 0),
                    )
            except Exception:
                logger.debug("route timing middleware failed to emit diag event", exc_info=True)


try:
    if app is not None and not getattr(app, "_sonder_route_timing_middleware", False):
        app.middlewares.append(_route_timing_middleware)
        setattr(app, "_sonder_route_timing_middleware", True)
except Exception:
    logger.debug("Could not install Sonder route timing middleware", exc_info=True)


def _coerce_nonnegative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


TRASH_RETENTION_DAYS = 30
MB_BYTES_DECIMAL = 1_000_000
_LOGGED_WARNING_KEYS = set()


def _query_nonnegative_int(value, default: int) -> int:
    if value is None or value == "":
        return default
    return _coerce_nonnegative_int(value, default)


def _query_optional_nonnegative_float(value, default=None):
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"", "null", "none", "unlimited"}:
        return None
    try:
        return max(0.0, float(normalized))
    except (TypeError, ValueError):
        return default


def _warn_once(key, message: str, *args) -> None:
    if key in _LOGGED_WARNING_KEYS:
        logger.debug(message, *args)
        return
    _LOGGED_WARNING_KEYS.add(key)
    logger.warning(message, *args)


def _summarize_ffmpeg_stderr(stderr: str, limit: int = 260) -> str:
    text = str(stderr or "").replace("\r", "\n")
    ignored_prefixes = (
        "ffmpeg version",
        "built with",
        "configuration:",
        "libavutil",
        "libavcodec",
        "libavformat",
        "libavdevice",
        "libavfilter",
        "libswscale",
        "libswresample",
        "libpostproc",
    )
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().lower().startswith(ignored_prefixes)
    ]
    if not lines:
        return "no ffmpeg stderr"

    preferred_markers = (
        "error",
        "failed",
        "invalid",
        "no such",
        "does not contain",
        "could not",
        "unable",
        "stream",
    )
    useful = [line for line in lines if any(marker in line.lower() for marker in preferred_markers)]
    summary = " | ".join(useful[-2:] if useful else lines[-2:])
    return summary[:limit]


def _ffmpeg_no_audio_stderr(stderr: str) -> bool:
    lowered = str(stderr or "").lower()
    return (
        "does not contain any stream" in lowered
        or "output file #0 does not contain any stream" in lowered
        or "stream map" in lowered and "matches no streams" in lowered
    )


def _resolve_source_path(source_path: str) -> str:
    return resolve_comfy_input_path(
        str(source_path or ""),
        purpose="comfy input source",
        must_exist=True,
    )


def _detect_asset_type(source_path: str, fallback: str = "video") -> str:
    asset_type, _artifact_kind = classify_asset_path(source_path)
    return asset_type or fallback


def _classify_asset_for_registration(source_path: str) -> tuple[str, str]:
    return classify_asset_path(source_path)


def _extract_asset_media_metadata(source_path: str, asset_type: str, *, strict: bool = False) -> dict:
    return probe_media_metadata(source_path, asset_type, strict=strict)


def _media_asset_requires_probe(asset_type: str) -> bool:
    return asset_type in {"video", "image", "audio"}


def _metadata_checked_for_asset(asset_type: str, metadata: dict) -> bool:
    return _media_asset_requires_probe(asset_type) and is_valid_media_metadata(metadata, asset_type)


def _finite_positive_number(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False
    return math.isfinite(number) and number > 0


def _valid_source_frame_count(asset: Asset) -> int:
    try:
        frame_count = int(float(getattr(asset, "frame_count", 0) or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    return frame_count if frame_count > 0 else 0


def _valid_audio_duration_frames(asset: Asset, fps: float) -> int:
    return media_timeline_frames(asset, fps) if getattr(asset, "asset_type", "") == "audio" else 0


def _asset_file_size(source_path: str) -> int:
    try:
        return os.path.getsize(source_path)
    except OSError:
        return 0


def _clip_source_asset_type(project: TimelineProject, clip: ClipReference) -> str:
    asset = next(
        (asset for asset in project.assets if asset.path == getattr(clip, "source_path", "")),
        None,
    )
    if asset:
        return getattr(asset, "asset_type", "") or ""
    return _detect_asset_type(getattr(clip, "source_path", ""), "")


def _is_render_clip(clip: ClipReference) -> bool:
    return getattr(clip, "role", "render") in ("", "render")


def _trim_lane_configs(configs: list, removed_index: int, target_count: int) -> None:
    if 0 <= removed_index < len(configs):
        configs.pop(removed_index)
    while len(configs) > target_count:
        configs.pop()
    while len(configs) < target_count:
        configs.append(LaneConfig())


def _compact_empty_media_lane(scene: Scene, lane_type: str, lane_index: int) -> bool:
    """Remove an empty normal video/audio lane and shift higher lanes down."""
    try:
        lane_index = int(lane_index)
    except (TypeError, ValueError):
        return False
    if lane_index < 0:
        return False

    if lane_type == "video":
        lane_count = max(1, int(scene.video_lane_count or 1))
        if lane_count <= 1 or lane_index >= lane_count:
            return False
        if any(_is_render_clip(clip) and (clip.track_index or 0) == lane_index for clip in scene.clips):
            return False
        for clip in scene.clips:
            if _is_render_clip(clip) and (clip.track_index or 0) > lane_index:
                clip.track_index = max(0, (clip.track_index or 0) - 1)
        scene.video_lane_count = lane_count - 1
        _trim_lane_configs(scene.video_lane_configs, lane_index, scene.video_lane_count)
        return True

    if lane_type == "audio":
        lane_count = max(1, int(scene.audio_lane_count or 1))
        if lane_count <= 1 or lane_index >= lane_count:
            return False
        if any((track.lane_index or 0) == lane_index for track in scene.audio_tracks):
            return False
        for track in scene.audio_tracks:
            if (track.lane_index or 0) > lane_index:
                track.lane_index = max(0, (track.lane_index or 0) - 1)
        scene.audio_lane_count = lane_count - 1
        _trim_lane_configs(scene.audio_lane_configs, lane_index, scene.audio_lane_count)
        return True

    return False


class ProjectMutationRequestError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = "invalid_project_mutation"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _mutation_error(message: str, status: int = 400, code: str = "invalid_project_mutation") -> None:
    raise ProjectMutationRequestError(message, status, code)


def _mutation_json_error(exc: ProjectMutationRequestError) -> web.Response:
    return web.json_response({"error": exc.message, "code": exc.code}, status=exc.status)


def _require_scene_queue_idle(project: TimelineProject, scene_id: str) -> None:
    for job in getattr(project, "generation_queue", []) or []:
        if str(getattr(job, "scene_id", "") or "") != str(scene_id or ""):
            continue
        status = str(getattr(job, "status", "pending") or "pending").lower()
        if status in {"pending", "running"}:
            _mutation_error(
                "Finish or clear queued jobs for this scene before changing FPS",
                409,
                "queue_jobs_pending",
            )


def _mutation_int(value, field_name: str, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        _mutation_error(f"Invalid integer for {field_name}: {value!r}", 400)


def _mutation_float(value, field_name: str, default: float | None = None) -> float:
    if value is None and default is not None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        _mutation_error(f"Invalid number for {field_name}: {value!r}", 400)


def _scene_lane_configs(scene: Scene, lane_type: str) -> list[LaneConfig]:
    if lane_type == "video":
        return scene.video_lane_configs
    if lane_type == "motion_driver":
        return scene.motion_driver_lane_configs
    if lane_type == "audio":
        return scene.audio_lane_configs
    _mutation_error(f"Unknown lane type: {lane_type}", 400)


def _scene_lane_count(scene: Scene, lane_type: str) -> int:
    if lane_type == "video":
        return max(1, int(scene.video_lane_count or 1))
    if lane_type == "motion_driver":
        return max(1, int(scene.motion_driver_lane_count or 1))
    if lane_type == "audio":
        return max(1, int(scene.audio_lane_count or 1))
    _mutation_error(f"Unknown lane type: {lane_type}", 400)


def _set_scene_lane_count(scene: Scene, lane_type: str, count: int) -> None:
    count = max(1, int(count))
    if lane_type == "video":
        scene.video_lane_count = count
        configs = scene.video_lane_configs
    elif lane_type == "motion_driver":
        scene.motion_driver_lane_count = count
        configs = scene.motion_driver_lane_configs
    elif lane_type == "audio":
        scene.audio_lane_count = count
        configs = scene.audio_lane_configs
    else:
        _mutation_error(f"Unknown lane type: {lane_type}", 400)
    while len(configs) < count:
        configs.append(LaneConfig())
    while len(configs) > count:
        configs.pop()


def _lane_config(scene: Scene, lane_type: str, lane_index: int) -> LaneConfig:
    lane_index = _mutation_int(lane_index, "lane_index")
    if lane_index < 0:
        _mutation_error("Lane index must be non-negative", 400)
    configs = _scene_lane_configs(scene, lane_type)
    while len(configs) <= lane_index:
        configs.append(LaneConfig())
    return configs[lane_index]


def _is_lane_config_locked(config: LaneConfig | None) -> bool:
    return bool(getattr(config, "locked", False)) if config is not None else False


def _require_lane_unlocked(scene: Scene, lane_type: str, lane_index: int | None = None) -> None:
    if lane_type == "guide":
        if _is_lane_config_locked(getattr(scene, "guide_track_config", None)):
            _mutation_error("Guide track is locked", 409, "track_locked")
        return
    if lane_type == "prompt":
        if _is_lane_config_locked(getattr(scene, "prompt_track_config", None)):
            _mutation_error("Prompt track is locked", 409, "track_locked")
        return
    if lane_type == "prompt_global":
        if _is_lane_config_locked(getattr(scene, "global_prompt_track_config", None)):
            _mutation_error("Global prompt track is locked", 409, "track_locked")
        return
    if lane_index is None:
        return
    if _is_lane_config_locked(_lane_config(scene, lane_type, lane_index)):
        _mutation_error("Lane is locked", 409, "track_locked")


def _clip_lane_type(clip: ClipReference) -> str:
    return "video" if _is_render_clip(clip) else "motion_driver"


def _require_clip_unlocked(scene: Scene, clip: ClipReference) -> None:
    _require_lane_unlocked(scene, _clip_lane_type(clip), int(getattr(clip, "track_index", 0) or 0))


def _require_audio_unlocked(scene: Scene, track: AudioTrack) -> None:
    _require_lane_unlocked(scene, "audio", int(getattr(track, "lane_index", 0) or 0))


def _ensure_scene_lane_config_lengths(scene: Scene) -> None:
    _set_scene_lane_count(scene, "video", _scene_lane_count(scene, "video"))
    _set_scene_lane_count(scene, "motion_driver", _scene_lane_count(scene, "motion_driver"))
    _set_scene_lane_count(scene, "audio", _scene_lane_count(scene, "audio"))


def _validate_single_driver_per_lane(scene: Scene) -> None:
    lane_count = _scene_lane_count(scene, "motion_driver")
    occupied: dict[int, str] = {}
    for clip in getattr(scene, "clips", []) or []:
        if getattr(clip, "role", "render") != "motion_driver":
            continue
        lane_index = int(getattr(clip, "track_index", 0) or 0)
        if lane_index < 0 or lane_index >= lane_count:
            _mutation_error(
                f"Driver clip is on missing driver lane {lane_index}",
                409,
                "driver_lane_missing",
            )
        prior_clip_id = occupied.get(lane_index)
        if prior_clip_id is not None:
            _mutation_error(
                "Only one driver clip is allowed per driver lane",
                409,
                "driver_lane_occupied",
            )
        occupied[lane_index] = getattr(clip, "clip_id", "") or ""


def _preflight_driver_lane_target(scene: Scene, clip: ClipReference, target_role: str, target_lane: int) -> None:
    if target_role != "motion_driver":
        return
    lane_count = _scene_lane_count(scene, "motion_driver")
    if target_lane < 0 or target_lane >= lane_count:
        _mutation_error(
            f"Driver clip is on missing driver lane {target_lane}",
            409,
            "driver_lane_missing",
        )
    clip_id = getattr(clip, "clip_id", "") or ""
    for other in getattr(scene, "clips", []) or []:
        if other is clip or (clip_id and getattr(other, "clip_id", "") == clip_id):
            continue
        if getattr(other, "role", "render") != "motion_driver":
            continue
        if int(getattr(other, "track_index", 0) or 0) == target_lane:
            _mutation_error(
                "Only one driver clip is allowed per driver lane",
                409,
                "driver_lane_occupied",
            )


def _find_clip(scene: Scene, clip_id: str) -> ClipReference:
    clip = next((c for c in scene.clips if c.clip_id == clip_id), None)
    if not clip:
        _mutation_error(f"Clip not found: {clip_id}", 404, "item_not_found")
    return clip


def _find_audio_track(scene: Scene, track_id: str) -> AudioTrack:
    track = next((t for t in scene.audio_tracks if t.track_id == track_id), None)
    if not track:
        _mutation_error(f"Audio track not found: {track_id}", 404, "item_not_found")
    return track


def _find_guide(scene: Scene, frame_index: int) -> GuideFrame:
    guide = next((g for g in scene.guide_frames if g.frame_index == frame_index), None)
    if not guide:
        _mutation_error(f"No guide at frame {frame_index}", 404, "item_not_found")
    return guide


def _find_guide_by_id(scene: Scene, guide_id: str) -> GuideFrame:
    guide = next((g for g in scene.guide_frames if getattr(g, "guide_id", "") == guide_id), None)
    if not guide:
        _mutation_error(f"Guide not found: {guide_id}", 404, "item_not_found")
    return guide


def _find_prompt_section(scene: Scene, index: int) -> PromptSection:
    if index < 0 or index >= len(scene.prompt_sections):
        _mutation_error(f"Prompt section index out of range: {index}", 404, "item_not_found")
    return scene.prompt_sections[index]


def _find_prompt_section_by_id(scene: Scene, prompt_id: str) -> PromptSection:
    section = next((p for p in scene.prompt_sections if getattr(p, "prompt_id", "") == prompt_id), None)
    if not section:
        _mutation_error(f"Prompt section not found: {prompt_id}", 404, "item_not_found")
    return section


def _prompt_section_index(scene: Scene, section: PromptSection) -> int:
    for index, current in enumerate(scene.prompt_sections):
        if current is section:
            return index
    _mutation_error("Prompt section not found", 404, "item_not_found")


def _expected_matches(value, expected) -> bool:
    if isinstance(value, float) or isinstance(expected, float):
        try:
            return abs(float(value) - float(expected)) <= 1e-9
        except (TypeError, ValueError):
            return False
    return value == expected


def _validate_guide_identity(guide: GuideFrame, expected: dict | None) -> None:
    if not isinstance(expected, dict):
        return
    checks = {
        "guide_id": getattr(guide, "guide_id", ""),
        "frame_index": getattr(guide, "frame_index", 0),
        "asset_id": getattr(guide, "asset_id", ""),
        "source": getattr(guide, "source", ""),
        "strength": getattr(guide, "strength", 1.0),
        "muted": bool(getattr(guide, "muted", False)),
    }
    for key, current in checks.items():
        if key in expected and not _expected_matches(current, expected[key]):
            _mutation_error("Guide identity mismatch", 409, "identity_mismatch")


def _validate_prompt_identity(section: PromptSection, expected: dict | None) -> None:
    if not isinstance(expected, dict):
        return
    checks = {
        "prompt_id": getattr(section, "prompt_id", ""),
        "start_frame": getattr(section, "start_frame", 0),
        "end_frame": getattr(section, "end_frame", 0),
        "prompt": getattr(section, "prompt", ""),
        "muted": bool(getattr(section, "muted", False)),
    }
    for key, current in checks.items():
        if key in expected and not _expected_matches(current, expected[key]):
            _mutation_error("Prompt section identity mismatch", 409, "identity_mismatch")
    if "channels" in expected:
        expected_channels = prompt_payload.normalize_channels(expected.get("channels"))
        if expected_channels != getattr(section, "channels", None):
            _mutation_error("Prompt section identity mismatch", 409, "identity_mismatch")


def _require_no_prompt_overlap(scene: Scene, start_frame: int, end_frame: int,
                               ignore=None) -> None:
    """Reject a section range that intersects another section (half-open).

    `ignore` may be a PromptSection (range updates) or a set of sections
    (swap final-state validation). Pre-existing stored overlaps are not
    auto-mutated — the resolver's first-wins clipping covers them at read
    time — but no NEW overlap may be created.
    """
    if end_frame <= start_frame:
        _mutation_error("Prompt section range is invalid", 400, "invalid_range")
    if isinstance(ignore, (list, tuple)):
        ignored = list(ignore)
    elif ignore is None:
        ignored = []
    else:
        ignored = [ignore]
    for other in scene.prompt_sections:
        # Identity comparison only — PromptSection __eq__ is value-based and
        # would skip a different-but-identical section.
        if any(other is item for item in ignored):
            continue
        if other.start_frame < end_frame and other.end_frame > start_frame:
            _mutation_error("Prompt sections cannot overlap", 409, "prompt_overlap")


LINK_ITEM_TYPES = {"clip", "audio", "guide", "prompt"}


def _link_ref(item_type: str, item_id) -> dict:
    return {"type": str(item_type or ""), "id": str(item_id or "")}


def _link_ref_key(ref: dict) -> tuple[str, str]:
    return (str(ref.get("type", "") or ""), str(ref.get("id", "") or ""))


def _scene_existing_link_ids(scene: Scene) -> dict[str, set[str]]:
    return {
        "clip": {clip.clip_id for clip in getattr(scene, "clips", []) or []},
        "audio": {track.track_id for track in getattr(scene, "audio_tracks", []) or []},
        "guide": {getattr(guide, "guide_id", "") for guide in getattr(scene, "guide_frames", []) or []},
        "prompt": {getattr(section, "prompt_id", "") for section in getattr(scene, "prompt_sections", []) or []},
    }


def _prune_linked_item_groups(scene: Scene) -> None:
    groups = getattr(scene, "linked_item_groups", None)
    if not isinstance(groups, list):
        scene.linked_item_groups = []
        return
    existing = _scene_existing_link_ids(scene)
    normalized = []
    seen_group_ids = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = str(group.get("group_id", "") or "")
        if not group_id or group_id in seen_group_ids:
            group_id = uuid.uuid4().hex[:8]
        items = []
        seen_items = set()
        for item in group.get("items", []) or []:
            if not isinstance(item, dict):
                continue
            item_type, item_id = _link_ref_key(item)
            key = (item_type, item_id)
            if item_type in LINK_ITEM_TYPES and item_id in existing.get(item_type, set()) and key not in seen_items:
                items.append(_link_ref(item_type, item_id))
                seen_items.add(key)
        if len(items) >= 2:
            normalized.append({"group_id": group_id, "items": items})
            seen_group_ids.add(group_id)
    scene.linked_item_groups = normalized


def _resolve_link_ref(scene: Scene, item_type: str, item_id: str):
    if item_type == "clip":
        return _find_clip(scene, item_id)
    if item_type == "audio":
        return _find_audio_track(scene, item_id)
    if item_type == "guide":
        return _find_guide_by_id(scene, item_id)
    if item_type == "prompt":
        return _find_prompt_section_by_id(scene, item_id)
    _mutation_error(f"Unsupported linked item type: {item_type}", 400, "invalid_link_type")


def _item_ref_from_selection(scene: Scene, item: dict) -> dict:
    if not isinstance(item, dict):
        _mutation_error("Linked item must be an object", 400)
    item_type = str(item.get("type", "") or "")
    if item_type not in LINK_ITEM_TYPES:
        _mutation_error(f"Unsupported linked item type: {item_type}", 400, "invalid_link_type")
    raw_id = item.get("id")
    if item_type == "guide" and not item.get("id") and "frame_index" in item:
        guide = _find_guide(scene, _mutation_int(item.get("frame_index"), "guide frame_index"))
        return _link_ref("guide", getattr(guide, "guide_id", ""))
    if item_type == "prompt" and not item.get("id") and "index" in item:
        section = _find_prompt_section(scene, _mutation_int(item.get("index"), "prompt index"))
        return _link_ref("prompt", getattr(section, "prompt_id", ""))
    if item_type == "prompt" and raw_id is not None:
        raw_id_str = str(raw_id)
        if raw_id_str.isdigit() and not any(getattr(p, "prompt_id", "") == raw_id_str for p in scene.prompt_sections):
            section = _find_prompt_section(scene, int(raw_id_str))
            return _link_ref("prompt", getattr(section, "prompt_id", ""))
    if item_type == "guide" and raw_id is not None:
        raw_id_str = str(raw_id)
        if not any(getattr(g, "guide_id", "") == raw_id_str for g in scene.guide_frames):
            try:
                guide = _find_guide(scene, int(raw_id_str))
                return _link_ref("guide", getattr(guide, "guide_id", ""))
            except (TypeError, ValueError):
                pass
    item_id = str(raw_id or "")
    _resolve_link_ref(scene, item_type, item_id)
    return _link_ref(item_type, item_id)


def _link_group_for_ref(scene: Scene, ref: dict) -> dict | None:
    key = _link_ref_key(ref)
    for group in getattr(scene, "linked_item_groups", []) or []:
        if any(_link_ref_key(item) == key for item in group.get("items", []) or []):
            return group
    return None


def _expand_linked_refs(scene: Scene, refs: list[dict], apply_linked: bool = True) -> list[dict]:
    _prune_linked_item_groups(scene)
    ordered = []
    seen = set()
    for ref in refs:
        key = _link_ref_key(ref)
        if key not in seen:
            ordered.append(_link_ref(*key))
            seen.add(key)
        if not apply_linked:
            continue
        group = _link_group_for_ref(scene, ref)
        if not group:
            continue
        for item in group.get("items", []) or []:
            item_key = _link_ref_key(item)
            if item_key not in seen:
                ordered.append(_link_ref(*item_key))
                seen.add(item_key)
    return ordered


def _require_link_ref_unlocked(scene: Scene, ref: dict) -> None:
    item_type, item_id = _link_ref_key(ref)
    item = _resolve_link_ref(scene, item_type, item_id)
    if item_type == "clip":
        _require_clip_unlocked(scene, item)
    elif item_type == "audio":
        _require_audio_unlocked(scene, item)
    elif item_type == "guide":
        _require_lane_unlocked(scene, "guide")
    elif item_type == "prompt":
        _require_lane_unlocked(scene, "prompt")


def _require_link_refs_unlocked(scene: Scene, refs: list[dict]) -> None:
    for ref in refs:
        _require_link_ref_unlocked(scene, ref)


def _item_bounds(scene: Scene, ref: dict) -> tuple[int, int]:
    item_type, item_id = _link_ref_key(ref)
    item = _resolve_link_ref(scene, item_type, item_id)
    if item_type in {"clip", "audio"}:
        return int(item.timeline_start_frame), int(item.timeline_end_frame)
    if item_type == "guide":
        idx = int(item.frame_index)
        return idx, idx + 1
    if item_type == "prompt":
        return int(item.start_frame), int(item.end_frame)
    _mutation_error(f"Unsupported linked item type: {item_type}", 400, "invalid_link_type")


def _add_link_group(scene: Scene, items: list[dict], group_id: str = "") -> dict:
    refs = []
    seen = set()
    for item in items:
        ref = _item_ref_from_selection(scene, item)
        key = _link_ref_key(ref)
        if key not in seen:
            refs.append(ref)
            seen.add(key)
    if len(refs) < 2:
        _mutation_error("A link group requires at least two existing items", 400, "invalid_link_group")
    _unlink_refs(scene, refs)
    group = {"group_id": str(group_id or uuid.uuid4().hex[:8]), "items": refs}
    scene.linked_item_groups.append(group)
    _prune_linked_item_groups(scene)
    return group


def _unlink_refs(scene: Scene, refs: list[dict]) -> None:
    keys = {_link_ref_key(ref) for ref in refs}
    next_groups = []
    for group in getattr(scene, "linked_item_groups", []) or []:
        items = [item for item in group.get("items", []) or [] if _link_ref_key(item) not in keys]
        if len(items) >= 2:
            next_groups.append({"group_id": group.get("group_id") or uuid.uuid4().hex[:8], "items": items})
    scene.linked_item_groups = next_groups


def _rewrite_link_groups_for_deleted(scene: Scene, refs: list[dict]) -> None:
    _unlink_refs(scene, refs)
    _prune_linked_item_groups(scene)


def _validate_prompt_target_ranges(scene: Scene, targets: dict[str, tuple[PromptSection, int, int]]) -> None:
    sections = [item[0] for item in targets.values()]
    ranges = []
    for section, start, end in targets.values():
        if end <= start:
            _mutation_error("Prompt section range is invalid", 400, "invalid_range")
        if start < 0 or (int(getattr(scene, "duration_frames", 0) or 0) > 0 and end > int(scene.duration_frames)):
            _mutation_error("Prompt section range is outside the scene", 409, "invalid_range")
        _require_no_prompt_overlap(scene, start, end, ignore=sections)
        ranges.append((section, start, end))
    for idx, (section, start, end) in enumerate(ranges):
        for other, other_start, other_end in ranges[idx + 1:]:
            if section is other:
                continue
            if start < other_end and end > other_start:
                _mutation_error("Prompt sections cannot overlap", 409, "prompt_overlap")


def _apply_ref_muted(scene: Scene, ref: dict, muted: bool) -> None:
    item_type, item_id = _link_ref_key(ref)
    item = _resolve_link_ref(scene, item_type, item_id)
    if item_type in {"clip", "audio", "guide", "prompt"}:
        item.muted = bool(muted)


def _apply_ref_bounds(scene: Scene, ref: dict, start: int, end: int,
                      old_start: int, old_end: int) -> None:
    item_type, item_id = _link_ref_key(ref)
    item = _resolve_link_ref(scene, item_type, item_id)
    if item_type == "clip":
        if start < 0 or end <= start:
            _mutation_error("Clip range is invalid", 400, "invalid_range")
        left_delta = start - old_start
        right_delta = end - old_end
        pure_move = left_delta == right_delta
        item.timeline_start_frame = start
        item.timeline_end_frame = end
        if left_delta and not pure_move:
            item.source_in_frame = int(getattr(item, "source_in_frame", 0) or 0) + left_delta
        if right_delta and not pure_move:
            item.source_out_frame = int(getattr(item, "source_out_frame", 0) or 0) + right_delta
        return
    if item_type == "audio":
        if start < 0 or end <= start:
            _mutation_error("Audio range is invalid", 400, "invalid_range")
        left_delta = start - old_start
        right_delta = end - old_end
        pure_move = left_delta == right_delta
        item.timeline_start_frame = start
        item.timeline_end_frame = end
        if left_delta and not pure_move:
            item.source_in_frame = int(getattr(item, "source_in_frame", 0) or 0) + left_delta
        return
    if item_type == "guide":
        frame = start
        duration = int(getattr(scene, "duration_frames", 0) or 0)
        if frame < 0 or (duration > 0 and frame >= duration):
            _mutation_error("Guide frame is outside the scene", 409, "invalid_range")
        item.frame_index = frame
        return
    if item_type == "prompt":
        item.start_frame = start
        item.end_frame = end
        scene.prompt_sections.sort(key=lambda section: section.start_frame)
        return


def _apply_linked_bounds_update(
    project: TimelineProject,
    scene: Scene,
    anchor_ref: dict,
    fields: dict,
    validate_lane_collision: bool = False,
) -> None:
    if not isinstance(fields, dict):
        _mutation_error("Linked update requires fields", 400)
    refs = _expand_linked_refs(scene, [anchor_ref], True)
    _require_link_refs_unlocked(scene, refs)

    item_type, item_id = _link_ref_key(anchor_ref)
    old_bounds = {tuple(_link_ref_key(ref)): _item_bounds(scene, ref) for ref in refs}
    anchor_old_start, anchor_old_end = old_bounds[(item_type, item_id)]

    if set(fields.keys()) <= {"muted"} and "muted" in fields:
        for ref in refs:
            _apply_ref_muted(scene, ref, bool(fields["muted"]))
        return

    if item_type == "clip":
        anchor_new_start = max(0, int(fields.get("timeline_start_frame", anchor_old_start)))
        anchor_new_end = int(fields.get("timeline_end_frame", anchor_old_end + (anchor_new_start - anchor_old_start)))
    elif item_type == "audio":
        anchor_new_start = max(0, int(fields.get("timeline_start_frame", anchor_old_start)))
        anchor_new_end = int(fields.get("timeline_end_frame", anchor_old_end + (anchor_new_start - anchor_old_start)))
    elif item_type == "guide":
        anchor_new_start = _mutation_int(fields.get("frame_index", anchor_old_start), "frame_index")
        anchor_new_end = anchor_new_start + 1
    elif item_type == "prompt":
        anchor_new_start = int(fields.get("start_frame", anchor_old_start))
        anchor_new_end = int(fields.get("end_frame", anchor_old_end))
    else:
        _mutation_error(f"Unsupported linked item type: {item_type}", 400)

    delta_start = anchor_new_start - anchor_old_start
    delta_end = anchor_new_end - anchor_old_end
    pure_move = delta_start == delta_end
    prompt_targets = {}
    target_bounds = {}
    for ref in refs:
        key = tuple(_link_ref_key(ref))
        ref_type, ref_id = key
        old_start, old_end = old_bounds[key]
        if pure_move:
            next_start = old_start + delta_start
            next_end = old_end + delta_end
        else:
            next_start = old_start + delta_start if old_start == anchor_old_start else old_start
            next_end = old_end + delta_end if old_end == anchor_old_end else old_end
            if next_start == old_start and next_end == old_end:
                continue
        target_bounds[key] = (next_start, next_end)
        if ref_type == "prompt":
            prompt_targets[ref_id] = (_resolve_link_ref(scene, ref_type, ref_id), next_start, next_end)

    _validate_prompt_target_ranges(scene, prompt_targets)
    media_targets = []
    for ref in refs:
        key = tuple(_link_ref_key(ref))
        ref_type, ref_id = key
        if ref_type not in {"clip", "audio"}:
            continue
        item = _find_clip(scene, ref_id) if ref_type == "clip" else _find_audio_track(scene, ref_id)
        next_start, next_end = target_bounds.get(key, old_bounds[key])
        lane_type, lane_index = _media_item_lane(item)
        media_targets.append((item, next_start, next_end, lane_type, lane_index))
    if validate_lane_collision and media_targets:
        _require_media_target_bounds_fit(scene, media_targets)
    for ref in refs:
        key = tuple(_link_ref_key(ref))
        if key not in target_bounds:
            continue
        old_start, old_end = old_bounds[key]
        next_start, next_end = target_bounds[key]
        _apply_ref_bounds(scene, ref, next_start, next_end, old_start, old_end)

    # Apply non-temporal anchor fields after linked temporal propagation.
    if item_type == "clip":
        anchor = _find_clip(scene, item_id)
        remaining = {key: value for key, value in fields.items()
                     if key not in {"timeline_start_frame", "timeline_end_frame", "muted"}}
        _apply_update_clip(project, scene, item_id, remaining)
        if "muted" in fields:
            anchor.muted = bool(fields["muted"])
    elif item_type == "audio":
        remaining = {key: value for key, value in fields.items()
                     if key not in {"timeline_start_frame", "timeline_end_frame", "muted"}}
        _apply_update_audio_track(scene, item_id, remaining)
        if "muted" in fields:
            _find_audio_track(scene, item_id).muted = bool(fields["muted"])
    elif item_type == "guide":
        guide = _find_guide_by_id(scene, item_id)
        for field in ("asset_id", "source", "strength", "muted"):
            if field in fields:
                setattr(guide, field, bool(fields[field]) if field == "muted" else fields[field])
    elif item_type == "prompt":
        section = _find_prompt_section_by_id(scene, item_id)
        if isinstance(fields.get("channels"), dict):
            section.channels = prompt_payload.normalize_channels(fields["channels"])
        elif "prompt" in fields:
            section.prompt = str(fields["prompt"])
        if "muted" in fields:
            section.muted = bool(fields["muted"])


def _split_clip_object(scene: Scene, clip: ClipReference, split_frame: int) -> ClipReference:
    if getattr(clip, "role", "render") == "motion_driver":
        _mutation_error("Driver clips cannot be split", 409, "driver_clip_split_refused")
    if split_frame <= clip.timeline_start_frame or split_frame >= clip.timeline_end_frame:
        _mutation_error("Split frame must be within clip range", 400, "invalid_range")
    source_offset = split_frame - clip.timeline_start_frame
    source_split = (clip.source_in_frame or 0) + source_offset
    orig_source_out = clip.source_out_frame or clip.timeline_end_frame - clip.timeline_start_frame
    left_source_in = clip.source_in_frame or 0
    right = ClipReference(
        source_path=clip.source_path,
        timeline_start_frame=split_frame,
        timeline_end_frame=clip.timeline_end_frame,
        source_in_frame=source_split,
        source_out_frame=orig_source_out,
        total_source_frames=orig_source_out - source_split,
        source_origin_frame=source_split,
        opacity=getattr(clip, "opacity", 1.0),
        track_index=clip.track_index,
        role=getattr(clip, "role", "render"),
        strength=getattr(clip, "strength", 1.0),
        muted=getattr(clip, "muted", False),
        fit_mode=getattr(clip, "fit_mode", "pad_edge"),
        crop_position=getattr(clip, "crop_position", "center"),
        prompt=getattr(clip, "prompt", ""),
        is_generated=getattr(clip, "is_generated", False),
        generation_params=dict(getattr(clip, "generation_params", {}) or {}),
        takes=list(getattr(clip, "takes", []) or []),
        active_take=int(getattr(clip, "active_take", 0) or 0),
        take_metadata=dict(getattr(clip, "take_metadata", {}) or {}),
    )
    clip.timeline_end_frame = split_frame
    clip.source_out_frame = source_split
    clip.total_source_frames = source_split - left_source_in
    clip.source_origin_frame = left_source_in
    scene.clips.append(right)
    return right


def _split_audio_object(scene: Scene, track: AudioTrack, split_frame: int) -> AudioTrack:
    if split_frame <= track.timeline_start_frame or split_frame >= track.timeline_end_frame:
        _mutation_error("Split frame must be within track range", 400, "invalid_range")
    source_offset = split_frame - track.timeline_start_frame
    source_split = (track.source_in_frame or 0) + source_offset
    orig_end_frame = track.timeline_end_frame
    right_duration = orig_end_frame - split_frame
    left_duration = split_frame - track.timeline_start_frame
    right = AudioTrack(
        source_path=track.source_path,
        timeline_start_frame=split_frame,
        timeline_end_frame=orig_end_frame,
        source_in_frame=source_split,
        total_source_frames=right_duration,
        source_origin_frame=source_split,
        volume=getattr(track, "volume", 1.0),
        muted=getattr(track, "muted", False),
        lane_index=track.lane_index,
    )
    track.timeline_end_frame = split_frame
    track.total_source_frames = left_duration
    track.source_origin_frame = track.source_in_frame or 0
    scene.audio_tracks.append(right)
    return right


def _split_prompt_object(scene: Scene, section: PromptSection, split_frame: int) -> PromptSection:
    if split_frame <= section.start_frame or split_frame >= section.end_frame:
        _mutation_error("Split frame must be within prompt section range", 400, "invalid_range")
    right = PromptSection(
        start_frame=split_frame,
        end_frame=section.end_frame,
        channels=dict(getattr(section, "channels", {}) or {}),
        muted=bool(getattr(section, "muted", False)),
    )
    section.end_frame = split_frame
    scene.prompt_sections.append(right)
    scene.prompt_sections.sort(key=lambda item: item.start_frame)
    return right


def _apply_split_linked(scene: Scene, anchor_ref: dict, split_frame: int, apply_linked: bool = True) -> dict:
    refs = _expand_linked_refs(scene, [anchor_ref], apply_linked)
    _require_link_refs_unlocked(scene, refs)
    split_frame = _mutation_int(split_frame, "frame")
    for ref in refs:
        item_type, item_id = _link_ref_key(ref)
        if item_type != "clip":
            continue
        clip = _find_clip(scene, item_id)
        start, end = _item_bounds(scene, ref)
        if start < split_frame < end and getattr(clip, "role", "render") == "motion_driver":
            _mutation_error("Driver clips cannot be split", 409, "driver_clip_split_refused")
    left_refs = []
    right_refs = []
    split_results = {}

    for ref in refs:
        item_type, item_id = _link_ref_key(ref)
        start, end = _item_bounds(scene, ref)
        if start < split_frame < end:
            if item_type == "clip":
                right = _split_clip_object(scene, _find_clip(scene, item_id), split_frame)
                left_refs.append(ref)
                right_ref = _link_ref("clip", right.clip_id)
                right_refs.append(right_ref)
                split_results[item_id] = {"left": ref, "right": right_ref}
            elif item_type == "audio":
                right = _split_audio_object(scene, _find_audio_track(scene, item_id), split_frame)
                left_refs.append(ref)
                right_ref = _link_ref("audio", right.track_id)
                right_refs.append(right_ref)
                split_results[item_id] = {"left": ref, "right": right_ref}
            elif item_type == "prompt":
                right = _split_prompt_object(scene, _find_prompt_section_by_id(scene, item_id), split_frame)
                left_refs.append(ref)
                right_ref = _link_ref("prompt", right.prompt_id)
                right_refs.append(right_ref)
                split_results[item_id] = {"left": ref, "right": right_ref}
            else:
                left_refs.append(ref)
        else:
            left_overlap = max(0, min(end, split_frame) - start)
            right_overlap = max(0, end - max(start, split_frame))
            if right_overlap > left_overlap:
                right_refs.append(ref)
            else:
                left_refs.append(ref)

    if apply_linked:
        _unlink_refs(scene, refs)
        if len(left_refs) >= 2:
            scene.linked_item_groups.append({"group_id": uuid.uuid4().hex[:8], "items": left_refs})
        if len(right_refs) >= 2:
            scene.linked_item_groups.append({"group_id": uuid.uuid4().hex[:8], "items": right_refs})
        _prune_linked_item_groups(scene)

    return {
        "type": "split_linked_items" if apply_linked else "split_item",
        "frame": split_frame,
        "split_count": len(split_results),
        "left_items": left_refs,
        "right_items": right_refs,
    }


def _apply_scene_fields(project: TimelineProject, scene: Scene, fields: dict) -> None:
    if not isinstance(fields, dict):
        _mutation_error("update_scene_fields requires fields", 400)
    if "name" in fields:
        scene.name = str(fields["name"])
    if "duration_frames" in fields:
        scene.duration_frames = max(0, int(fields["duration_frames"]))
    if "prompt" in fields:
        _require_lane_unlocked(scene, "prompt_global")
        scene.prompt = str(fields["prompt"])
    if "generation_params" in fields:
        scene.generation_params = fields["generation_params"] if isinstance(fields["generation_params"], dict) else {}
    if "width" in fields:
        scene.width = max(0, int(fields["width"]))
    if "height" in fields:
        scene.height = max(0, int(fields["height"]))
    if "fps" in fields:
        old_fps = effective_scene_fps(project, scene)
        new_scene_fps = max(0.0, float(fields["fps"]))
        previous_scene_fps = scene.fps
        scene.fps = new_scene_fps
        new_fps = effective_scene_fps(project, scene)
        scene.fps = previous_scene_fps
        if new_fps != old_fps:
            _require_scene_queue_idle(project, scene.scene_id)
            retime_scene_geometry(scene, old_fps, new_fps)
        scene.fps = new_scene_fps
    if "video_lane_count" in fields:
        _set_scene_lane_count(scene, "video", max(1, int(fields["video_lane_count"])))
    if "motion_driver_lane_count" in fields:
        _set_scene_lane_count(scene, "motion_driver", max(1, int(fields["motion_driver_lane_count"])))
    if "audio_lane_count" in fields:
        _set_scene_lane_count(scene, "audio", max(1, int(fields["audio_lane_count"])))
    _ensure_scene_lane_config_lengths(scene)


def _apply_lane_configs(scene: Scene, fields: dict) -> None:
    if not isinstance(fields, dict):
        _mutation_error("update_lane_configs requires fields", 400)
    if "video_lane_configs" in fields:
        scene.video_lane_configs = [LaneConfig.from_dict(c) for c in fields["video_lane_configs"]]
    if "motion_driver_lane_configs" in fields:
        scene.motion_driver_lane_configs = [LaneConfig.from_dict(c) for c in fields["motion_driver_lane_configs"]]
    if "audio_lane_configs" in fields:
        scene.audio_lane_configs = [LaneConfig.from_dict(c) for c in fields["audio_lane_configs"]]
    if "guide_track_config" in fields:
        scene.guide_track_config = LaneConfig.from_dict(fields["guide_track_config"])
    if "prompt_track_config" in fields:
        scene.prompt_track_config = LaneConfig.from_dict(fields["prompt_track_config"])
    if "global_prompt_track_config" in fields:
        scene.global_prompt_track_config = LaneConfig.from_dict(fields["global_prompt_track_config"])
    _ensure_scene_lane_config_lengths(scene)


_FIXED_TRACK_CONFIG_ATTRS = {
    "guide": "guide_track_config",
    "prompt": "prompt_track_config",
    "prompt_global": "global_prompt_track_config",
}


def _apply_lane_config(scene: Scene, op: dict) -> dict:
    """Scoped per-lane config update (mutation-integrity F1).

    Unlike the legacy full-replace `update_lane_configs`, this writes exactly
    one lane/track config so a client can never clobber lanes it did not touch.
    Deliberately NO `_require_lane_unlocked`: toggling `locked` itself must
    work on a locked lane (matches the legacy op, which has no lock gate).
    """
    fields = op.get("fields")
    if not isinstance(fields, dict):
        _mutation_error("update_lane_config requires fields", 400)
    lane_type = str(op.get("lane_type", ""))
    if lane_type in _FIXED_TRACK_CONFIG_ATTRS:
        attr = _FIXED_TRACK_CONFIG_ATTRS[lane_type]
        config = getattr(scene, attr, None)
        if config is None:
            config = LaneConfig()
            setattr(scene, attr, config)
        lane_index = 0
    elif lane_type in {"video", "motion_driver", "audio"}:
        lane_index = _mutation_int(op.get("lane_index", 0), "lane_index", 0)
        if lane_index < 0 or lane_index >= _scene_lane_count(scene, lane_type):
            _mutation_error(f"Lane index out of range: {lane_index}", 404, "item_not_found")
        config = _lane_config(scene, lane_type, lane_index)
    else:
        _mutation_error(f"Unknown lane type: {lane_type}", 400)
    if "name" in fields:
        config.name = str(fields["name"] or "")
    if "color" in fields:
        config.color = str(fields["color"] or "")
    if "locked" in fields:
        config.locked = bool(fields["locked"])
    if "hidden" in fields:
        config.hidden = bool(fields["hidden"])
    return {"type": "update_lane_config", "lane_type": lane_type, "lane_index": lane_index}


def _media_lane_items(scene: Scene, lane_type: str, lane_index: int) -> list:
    if lane_type == "video":
        return [
            clip for clip in scene.clips
            if _is_render_clip(clip) and int(clip.track_index or 0) == lane_index
        ]
    if lane_type == "motion_driver":
        return [
            clip for clip in scene.clips
            if getattr(clip, "role", "render") == "motion_driver"
            and int(clip.track_index or 0) == lane_index
        ]
    if lane_type == "audio":
        return [
            track for track in scene.audio_tracks
            if int(track.lane_index or 0) == lane_index
        ]
    _mutation_error(f"Unknown lane type: {lane_type}", 400)


def _media_item_id(item) -> str:
    return str(getattr(item, "clip_id", "") or getattr(item, "track_id", "") or "")


def _media_items_overlap(left, right) -> bool:
    return _media_bounds_overlap(
        int(getattr(left, "timeline_start_frame", 0) or 0),
        int(getattr(left, "timeline_end_frame", 0) or 0),
        int(getattr(right, "timeline_start_frame", 0) or 0),
        int(getattr(right, "timeline_end_frame", 0) or 0),
    )


def _media_bounds_overlap(left_start: int, left_end: int, right_start: int, right_end: int) -> bool:
    return left_start < right_end and left_end > right_start


def _media_item_lane(item) -> tuple[str, int]:
    if isinstance(item, ClipReference):
        return _clip_lane_type(item), int(getattr(item, "track_index", 0) or 0)
    return "audio", int(getattr(item, "lane_index", 0) or 0)


def _require_media_target_bounds_fit(scene: Scene, targets: list[tuple[object, int, int, str, int]]) -> None:
    target_ids = {_media_item_id(item) for item, *_rest in targets}
    for index, (item, start, end, lane_type, lane_index) in enumerate(targets):
        if end <= start:
            _mutation_error("Timeline item range must be at least one frame", 409, "invalid_range")
        for other, other_start, other_end, other_lane_type, other_lane_index in targets[index + 1:]:
            if lane_type != other_lane_type or lane_index != other_lane_index:
                continue
            if _media_bounds_overlap(start, end, other_start, other_end):
                _mutation_error("Timeline items cannot overlap on one lane", 409, "lane_collision")
        for other in _media_lane_items(scene, lane_type, lane_index):
            if _media_item_id(other) in target_ids:
                continue
            if _media_bounds_overlap(
                start,
                end,
                int(getattr(other, "timeline_start_frame", 0) or 0),
                int(getattr(other, "timeline_end_frame", 0) or 0),
            ):
                _mutation_error("Timeline item overlaps another item on the lane", 409, "lane_collision")


def _require_media_items_fit_lane(moving_items: list, destination_items: list) -> None:
    for index, item in enumerate(moving_items):
        for other in moving_items[index + 1:]:
            if _media_items_overlap(item, other):
                _mutation_error(
                    "Selected items overlap and cannot share one lane",
                    409,
                    "lane_collision",
                )
        for other in destination_items:
            if _media_items_overlap(item, other):
                _mutation_error(
                    "Items overlap the destination lane",
                    409,
                    "lane_collision",
                )


def _consolidate_media_items(scene: Scene, op: dict) -> dict:
    lane_type = str(op.get("lane_type", ""))
    if lane_type not in {"video", "audio"}:
        _mutation_error("Consolidation supports render-video or audio items only", 400, "invalid_consolidation")

    raw_ids = op.get("item_ids", [])
    if not isinstance(raw_ids, list):
        _mutation_error("item_ids must be a list", 400, "invalid_consolidation")
    item_ids = [str(item_id or "") for item_id in raw_ids]
    if len(item_ids) < 2 or any(not item_id for item_id in item_ids):
        _mutation_error("Select at least two items to consolidate", 400, "invalid_consolidation")
    if len(set(item_ids)) != len(item_ids):
        _mutation_error("Consolidation item IDs must be unique", 400, "invalid_consolidation")

    lane_count = _scene_lane_count(scene, lane_type)
    target_lane = _mutation_int(op.get("target_lane"), "target_lane")
    if target_lane < 0 or target_lane >= lane_count:
        _mutation_error("Consolidation target lane is out of range", 400, "invalid_consolidation")

    if lane_type == "video":
        selected_items = [_find_clip(scene, item_id) for item_id in item_ids]
        if any(not _is_render_clip(item) for item in selected_items):
            _mutation_error("Driver clips cannot be consolidated", 400, "invalid_consolidation")
        item_lane = lambda item: int(item.track_index or 0)
    else:
        selected_items = [_find_audio_track(scene, item_id) for item_id in item_ids]
        item_lane = lambda item: int(item.lane_index or 0)

    source_lanes = {item_lane(item) for item in selected_items}
    if source_lanes == {target_lane}:
        _mutation_error("Selected items are already on the destination lane", 400, "invalid_consolidation")
    for lane_index in source_lanes | {target_lane}:
        if lane_index < 0 or lane_index >= lane_count:
            _mutation_error("Selected item is on a missing lane", 409, "invalid_consolidation")
        _require_lane_unlocked(scene, lane_type, lane_index)

    selected_ids = set(item_ids)
    destination_items = [
        item for item in _media_lane_items(scene, lane_type, target_lane)
        if _media_item_id(item) not in selected_ids
    ]
    _require_media_items_fit_lane(selected_items, destination_items)

    for item in selected_items:
        if lane_type == "video":
            item.track_index = target_lane
        else:
            item.lane_index = target_lane

    removed_lanes: list[int] = []
    if bool(op.get("remove_vacated_lanes", False)):
        for source_lane in sorted(source_lanes - {target_lane}, reverse=True):
            if _media_lane_items(scene, lane_type, source_lane):
                continue
            _remove_media_lane(scene, lane_type, source_lane, "require_empty")
            removed_lanes.append(source_lane)

    final_target_lane = target_lane - sum(1 for lane_index in removed_lanes if lane_index < target_lane)
    return {
        "type": "consolidate_items",
        "lane_type": lane_type,
        "moved_count": len(selected_items),
        "removed_lanes": sorted(removed_lanes),
        "target_lane": final_target_lane,
    }


def _remove_media_lane(scene: Scene, lane_type: str, lane_index: int, item_policy: str, target_lane: int | None = None) -> None:
    if lane_type not in {"video", "motion_driver", "audio"}:
        _mutation_error(f"Cannot remove lane type: {lane_type}", 400)
    lane_index = _mutation_int(lane_index, "lane_index")
    current_count = _scene_lane_count(scene, lane_type)
    if current_count <= 1:
        _mutation_error("Cannot remove the only lane", 409, "invalid_lane_operation")
    if lane_index < 0 or lane_index >= current_count:
        _mutation_error(f"Lane index out of range: {lane_index}", 404, "item_not_found")
    _require_lane_unlocked(scene, lane_type, lane_index)

    lane_items = _media_lane_items(scene, lane_type, lane_index)

    item_policy = str(item_policy or "require_empty")
    if lane_items and item_policy == "require_empty":
        _mutation_error("Lane is not empty", 409, "lane_not_empty")

    if lane_items and item_policy == "move_items":
        if target_lane is None:
            target_lane = lane_index - 1 if lane_index > 0 else 1
        target_lane = _mutation_int(target_lane, "target_lane")
        if target_lane < 0 or target_lane >= current_count or target_lane == lane_index:
            _mutation_error("Invalid target lane", 400)
        _require_lane_unlocked(scene, lane_type, target_lane)
        _require_media_items_fit_lane(
            lane_items,
            _media_lane_items(scene, lane_type, target_lane),
        )
        for item in lane_items:
            if lane_type in {"video", "motion_driver"}:
                item.track_index = target_lane
            else:
                item.lane_index = target_lane
    elif lane_items and item_policy == "delete_items":
        if lane_type in {"video", "motion_driver"}:
            deleting = {item.clip_id for item in lane_items}
            scene.clips = [clip for clip in scene.clips if clip.clip_id not in deleting]
        else:
            deleting = {item.track_id for item in lane_items}
            scene.audio_tracks = [track for track in scene.audio_tracks if track.track_id not in deleting]
    elif lane_items:
        _mutation_error(f"Unknown lane item policy: {item_policy}", 400)

    if lane_type == "video":
        for clip in scene.clips:
            if _is_render_clip(clip) and int(clip.track_index or 0) > lane_index:
                clip.track_index = max(0, int(clip.track_index or 0) - 1)
        scene.video_lane_count = current_count - 1
        _trim_lane_configs(scene.video_lane_configs, lane_index, scene.video_lane_count)
    elif lane_type == "motion_driver":
        for clip in scene.clips:
            if getattr(clip, "role", "render") == "motion_driver" and int(clip.track_index or 0) > lane_index:
                clip.track_index = max(0, int(clip.track_index or 0) - 1)
        scene.motion_driver_lane_count = current_count - 1
        _trim_lane_configs(scene.motion_driver_lane_configs, lane_index, scene.motion_driver_lane_count)
        _validate_single_driver_per_lane(scene)
    else:
        for track in scene.audio_tracks:
            if int(track.lane_index or 0) > lane_index:
                track.lane_index = max(0, int(track.lane_index or 0) - 1)
        scene.audio_lane_count = current_count - 1
        _trim_lane_configs(scene.audio_lane_configs, lane_index, scene.audio_lane_count)


def _validated_fit_mode(value) -> str:
    mode = str(value or "").strip().lower()
    if mode not in FIT_MODES:
        _mutation_error(f"Invalid fit_mode: {value}", 400, "invalid_fit_mode")
    return mode


def _validated_crop_position(value) -> str:
    pos = str(value or "").strip().lower()
    if pos not in CROP_POSITIONS:
        _mutation_error(f"Invalid crop_position: {value}", 400, "invalid_crop_position")
    return pos


def _apply_update_clip(
    project: TimelineProject,
    scene: Scene,
    clip_id: str,
    fields: dict,
    validate_lane_collision: bool = False,
) -> ClipReference:
    if not isinstance(fields, dict):
        _mutation_error("update_clip requires fields", 400)
    clip = _find_clip(scene, clip_id)
    _require_clip_unlocked(scene, clip)
    target_role = fields.get("role", getattr(clip, "role", "render"))
    if "role" in fields:
        if target_role not in {"render", "motion_driver"}:
            _mutation_error(f"Invalid clip role: {target_role}", 400)
        if target_role == "motion_driver" and _clip_source_asset_type(project, clip) != "video":
            _mutation_error("Driver clips require video assets", 400)
    target_lane = int(fields.get("track_index", getattr(clip, "track_index", 0)) or 0)
    _preflight_driver_lane_target(scene, clip, target_role, target_lane)
    if "track_index" in fields:
        target_lane_type = "video" if target_role in {"", "render"} else "motion_driver"
        _require_lane_unlocked(scene, target_lane_type, target_lane)

    if validate_lane_collision and any(key in fields for key in ("timeline_start_frame", "timeline_end_frame", "track_index", "role")):
        proposed_start = max(0, int(fields.get("timeline_start_frame", clip.timeline_start_frame)))
        if "timeline_start_frame" in fields and "timeline_end_frame" not in fields:
            proposed_end = proposed_start + (clip.timeline_end_frame - clip.timeline_start_frame)
        else:
            proposed_end = int(fields.get("timeline_end_frame", clip.timeline_end_frame))
        target_lane_type = "video" if target_role in {"", "render"} else "motion_driver"
        _require_media_target_bounds_fit(
            scene,
            [(clip, proposed_start, proposed_end, target_lane_type, target_lane)],
        )

    if "timeline_start_frame" in fields:
        new_start = max(0, int(fields["timeline_start_frame"]))
        if "timeline_end_frame" not in fields:
            duration = clip.timeline_end_frame - clip.timeline_start_frame
            clip.timeline_start_frame = new_start
            clip.timeline_end_frame = clip.timeline_start_frame + duration
        else:
            clip.timeline_start_frame = new_start
    if "timeline_end_frame" in fields:
        clip.timeline_end_frame = int(fields["timeline_end_frame"])
    if "source_in_frame" in fields:
        clip.source_in_frame = int(fields["source_in_frame"])
    if "source_out_frame" in fields:
        clip.source_out_frame = int(fields["source_out_frame"])
    if "opacity" in fields:
        clip.opacity = float(fields["opacity"])
    if "track_index" in fields:
        clip.track_index = int(fields["track_index"])
    if "role" in fields:
        clip.role = target_role
    if "strength" in fields:
        clip.strength = float(fields["strength"])
    if "muted" in fields:
        clip.muted = bool(fields["muted"])
    if "fit_mode" in fields:
        clip.fit_mode = _validated_fit_mode(fields["fit_mode"])
    if "crop_position" in fields:
        clip.crop_position = _validated_crop_position(fields["crop_position"])
    _validate_single_driver_per_lane(scene)
    return clip


def _apply_update_audio_track(
    scene: Scene,
    track_id: str,
    fields: dict,
    validate_lane_collision: bool = False,
) -> AudioTrack:
    if not isinstance(fields, dict):
        _mutation_error("update_audio_track requires fields", 400)
    track = _find_audio_track(scene, track_id)
    _require_audio_unlocked(scene, track)
    if "lane_index" in fields:
        _require_lane_unlocked(scene, "audio", int(fields["lane_index"]))

    if validate_lane_collision and any(key in fields for key in ("timeline_start_frame", "timeline_end_frame", "lane_index")):
        proposed_start = max(0, int(fields.get("timeline_start_frame", track.timeline_start_frame)))
        if "timeline_start_frame" in fields and "timeline_end_frame" not in fields:
            proposed_end = proposed_start + (track.timeline_end_frame - track.timeline_start_frame)
        else:
            proposed_end = int(fields.get("timeline_end_frame", track.timeline_end_frame))
        proposed_lane = int(fields.get("lane_index", track.lane_index) or 0)
        _require_media_target_bounds_fit(
            scene,
            [(track, proposed_start, proposed_end, "audio", proposed_lane)],
        )

    if "timeline_start_frame" in fields:
        new_start = max(0, int(fields["timeline_start_frame"]))
        if "timeline_end_frame" not in fields:
            duration = track.timeline_end_frame - track.timeline_start_frame
            track.timeline_start_frame = new_start
            track.timeline_end_frame = track.timeline_start_frame + duration
        else:
            track.timeline_start_frame = new_start
    if "timeline_end_frame" in fields:
        track.timeline_end_frame = int(fields["timeline_end_frame"])
    if "source_in_frame" in fields:
        track.source_in_frame = int(fields["source_in_frame"])
    if "muted" in fields:
        track.muted = bool(fields["muted"])
    if "volume" in fields:
        track.volume = float(fields["volume"])
    if "lane_index" in fields:
        track.lane_index = int(fields["lane_index"])
    return track


def _replacement_source_range(start_frame: int, end_frame: int, source_in_frame: int, total_source_frames: int) -> tuple[int, int, int]:
    total = max(0, int(total_source_frames or 0))
    if total <= 0:
        _mutation_error("Replacement asset has no usable duration", 400, "invalid_source_asset")
    old_duration = max(1, int(end_frame or 0) - int(start_frame or 0))
    old_source_in = max(0, int(source_in_frame or 0))
    source_in = old_source_in if old_source_in < total else 0
    visible_duration = min(old_duration, max(1, total - source_in))
    return source_in, source_in + visible_duration, visible_duration


def _resolve_replacement_asset(project: TimelineProject, asset_id: str, expected_type: str) -> Asset:
    asset_id = str(asset_id or "").strip()
    if not asset_id:
        _mutation_error("asset_id is required", 400, "invalid_source_asset")
    asset = project.get_asset(asset_id)
    if not asset:
        _mutation_error(f"Asset not found: {asset_id}", 404, "item_not_found")
    if _asset_is_trashed(asset):
        _mutation_error("Replacement asset is in Trash", 409, "asset_trashed")
    if getattr(asset, "asset_type", "") != expected_type:
        _mutation_error(f"Replacement asset must be {expected_type}", 400, "invalid_source_asset")
    return asset


def _apply_replace_clip_source(project: TimelineProject, scene: Scene, clip_id: str, asset_id: str) -> ClipReference:
    clip = _find_clip(scene, clip_id)
    _require_clip_unlocked(scene, clip)
    asset = _resolve_replacement_asset(project, asset_id, "video")
    if _valid_source_frame_count(asset) <= 0 and not _finite_positive_number(getattr(asset, "duration_sec", 0.0)):
        _mutation_error("Replacement video has no usable duration", 400, "invalid_source_asset")
    frame_count = media_timeline_frames(asset, effective_scene_fps(project, scene))
    if frame_count <= 0:
        _mutation_error("Replacement video has no usable duration", 400, "invalid_source_asset")

    source_in, source_out, visible_duration = _replacement_source_range(
        clip.timeline_start_frame,
        clip.timeline_end_frame,
        clip.source_in_frame,
        frame_count,
    )
    clip.source_path = asset.path
    clip.source_in_frame = source_in
    clip.source_out_frame = source_out
    clip.total_source_frames = frame_count
    clip.source_origin_frame = 0
    clip.timeline_end_frame = int(clip.timeline_start_frame or 0) + visible_duration
    clip.is_generated = False
    clip.generation_params = {}
    clip.takes = []
    clip.active_take = 0
    clip.take_metadata = {}
    _validate_single_driver_per_lane(scene)
    return clip


def _apply_replace_audio_source(project: TimelineProject, scene: Scene, track_id: str, asset_id: str) -> AudioTrack:
    track = _find_audio_track(scene, track_id)
    _require_audio_unlocked(scene, track)
    asset = _resolve_replacement_asset(project, asset_id, "audio")
    duration_frames = _valid_audio_duration_frames(asset, effective_scene_fps(project, scene))
    if duration_frames <= 0:
        _mutation_error("Replacement audio has no duration", 400, "invalid_source_asset")

    source_in, _source_out, visible_duration = _replacement_source_range(
        track.timeline_start_frame,
        track.timeline_end_frame,
        track.source_in_frame,
        duration_frames,
    )
    track.source_path = asset.path
    track.source_in_frame = source_in
    track.total_source_frames = duration_frames
    track.source_origin_frame = 0
    track.timeline_end_frame = int(track.timeline_start_frame or 0) + visible_duration
    return track


def _delete_clip(scene: Scene, clip_id: str, preserve_lane: bool = False) -> None:
    clip = _find_clip(scene, clip_id)
    _require_clip_unlocked(scene, clip)
    deleted_lane = int(clip.track_index or 0)
    should_compact = _is_render_clip(clip) and not preserve_lane
    scene.clips = [item for item in scene.clips if item.clip_id != clip_id]
    _rewrite_link_groups_for_deleted(scene, [_link_ref("clip", clip_id)])
    if should_compact:
        _compact_empty_media_lane(scene, "video", deleted_lane)


def _delete_audio_track(scene: Scene, track_id: str, preserve_lane: bool = False) -> None:
    track = _find_audio_track(scene, track_id)
    _require_audio_unlocked(scene, track)
    deleted_lane = int(track.lane_index or 0)
    scene.audio_tracks = [item for item in scene.audio_tracks if item.track_id != track_id]
    _rewrite_link_groups_for_deleted(scene, [_link_ref("audio", track_id)])
    if not preserve_lane:
        _compact_empty_media_lane(scene, "audio", deleted_lane)


def _apply_bulk_delete_items(scene: Scene, items: list, preserve_lanes: bool = False,
                             apply_linked: bool = False) -> None:
    if not isinstance(items, list):
        _mutation_error("bulk_delete_items requires items", 400)
    if apply_linked:
        refs = [_item_ref_from_selection(scene, item) for item in items]
        _apply_delete_link_refs(scene, refs, preserve_lanes)
        return
    resolved = []
    for item in items:
        if not isinstance(item, dict):
            _mutation_error("bulk_delete_items item must be an object", 400)
        item_type = str(item.get("type", ""))
        item_id = item.get("id")
        expected = item.get("expected")
        if item_type == "clip":
            clip = _find_clip(scene, str(item_id))
            _require_clip_unlocked(scene, clip)
            resolved.append((item_type, clip, bool(item.get("preserve_lane", preserve_lanes))))
        elif item_type == "audio":
            track = _find_audio_track(scene, str(item_id))
            _require_audio_unlocked(scene, track)
            resolved.append((item_type, track, bool(item.get("preserve_lane", preserve_lanes))))
        elif item_type == "guide":
            _require_lane_unlocked(scene, "guide")
            frame_index = _mutation_int(item_id, "guide frame_index")
            guide = _find_guide(scene, frame_index)
            _validate_guide_identity(guide, expected)
            resolved.append((item_type, guide, True))
        elif item_type == "prompt":
            _require_lane_unlocked(scene, "prompt")
            index = _mutation_int(item_id, "prompt index")
            section = _find_prompt_section(scene, index)
            _validate_prompt_identity(section, expected)
            resolved.append((item_type, index, True))
        else:
            _mutation_error(f"Unsupported bulk delete item type: {item_type}", 400)

    video_lanes = []
    audio_lanes = []
    prompt_indexes = []
    guide_frames = set()
    for item_type, item, preserve_lane in resolved:
        if item_type == "clip":
            video_lanes.append(int(item.track_index or 0))
            scene.clips = [clip for clip in scene.clips if clip.clip_id != item.clip_id]
        elif item_type == "audio":
            audio_lanes.append(int(item.lane_index or 0))
            scene.audio_tracks = [track for track in scene.audio_tracks if track.track_id != item.track_id]
        elif item_type == "guide":
            guide_frames.add(int(item.frame_index))
        elif item_type == "prompt":
            prompt_indexes.append(int(item))
        if preserve_lane:
            if item_type == "clip" and video_lanes:
                video_lanes.pop()
            if item_type == "audio" and audio_lanes:
                audio_lanes.pop()

    if guide_frames:
        scene.guide_frames = [guide for guide in scene.guide_frames if int(guide.frame_index) not in guide_frames]
    for idx in sorted(set(prompt_indexes), reverse=True):
        if 0 <= idx < len(scene.prompt_sections):
            scene.prompt_sections.pop(idx)
    for lane in sorted(set(video_lanes), reverse=True):
        _compact_empty_media_lane(scene, "video", lane)
    for lane in sorted(set(audio_lanes), reverse=True):
        _compact_empty_media_lane(scene, "audio", lane)
    _prune_linked_item_groups(scene)


def _apply_delete_link_refs(scene: Scene, refs: list[dict], preserve_lanes: bool = False) -> None:
    refs = _expand_linked_refs(scene, refs, True)
    _require_link_refs_unlocked(scene, refs)
    video_lanes = []
    audio_lanes = []
    prompt_ids = set()
    guide_ids = set()
    clip_ids = set()
    audio_ids = set()
    for ref in refs:
        item_type, item_id = _link_ref_key(ref)
        if item_type == "clip":
            clip = _find_clip(scene, item_id)
            if _is_render_clip(clip):
                video_lanes.append(int(getattr(clip, "track_index", 0) or 0))
            clip_ids.add(item_id)
        elif item_type == "audio":
            track = _find_audio_track(scene, item_id)
            audio_lanes.append(int(getattr(track, "lane_index", 0) or 0))
            audio_ids.add(item_id)
        elif item_type == "guide":
            _find_guide_by_id(scene, item_id)
            guide_ids.add(item_id)
        elif item_type == "prompt":
            _find_prompt_section_by_id(scene, item_id)
            prompt_ids.add(item_id)

    if clip_ids:
        scene.clips = [clip for clip in scene.clips if clip.clip_id not in clip_ids]
    if audio_ids:
        scene.audio_tracks = [track for track in scene.audio_tracks if track.track_id not in audio_ids]
    if guide_ids:
        scene.guide_frames = [guide for guide in scene.guide_frames if getattr(guide, "guide_id", "") not in guide_ids]
    if prompt_ids:
        scene.prompt_sections = [
            section for section in scene.prompt_sections
            if getattr(section, "prompt_id", "") not in prompt_ids
        ]

    if not preserve_lanes:
        for lane in sorted(set(video_lanes), reverse=True):
            _compact_empty_media_lane(scene, "video", lane)
        for lane in sorted(set(audio_lanes), reverse=True):
            _compact_empty_media_lane(scene, "audio", lane)
    _rewrite_link_groups_for_deleted(scene, refs)


def _apply_move_guide(scene: Scene, op: dict) -> GuideFrame:
    _require_lane_unlocked(scene, "guide")
    old_frame = _mutation_int(op.get("from_frame_index"), "from_frame_index")
    new_frame = _mutation_int(op.get("to_frame_index"), "to_frame_index")
    guide = _find_guide(scene, old_frame)
    _validate_guide_identity(guide, op.get("expected"))
    next_guide = GuideFrame(
        guide_id=str(op.get("guide_id", getattr(guide, "guide_id", "")) or getattr(guide, "guide_id", "") or uuid.uuid4().hex[:8]),
        frame_index=new_frame,
        asset_id=str(op.get("asset_id", getattr(guide, "asset_id", "")) or ""),
        source=str(op.get("source", getattr(guide, "source", "") or "asset") or "asset"),
        strength=_mutation_float(op.get("strength", getattr(guide, "strength", 1.0)), "strength", 1.0),
        muted=bool(op.get("muted", getattr(guide, "muted", False))),
        fit_mode=_validated_fit_mode(op["fit_mode"]) if "fit_mode" in op else getattr(guide, "fit_mode", "pad_edge"),
        crop_position=_validated_crop_position(op["crop_position"]) if "crop_position" in op else getattr(guide, "crop_position", "center"),
    )
    replaced_ids = [
        getattr(current, "guide_id", "")
        for current in scene.guide_frames
        if current.frame_index == new_frame and current is not guide
    ]
    scene.guide_frames = [
        current for current in scene.guide_frames
        if current.frame_index not in {old_frame, new_frame}
    ]
    scene.guide_frames.append(next_guide)
    scene.guide_frames.sort(key=lambda g: g.frame_index)
    if replaced_ids:
        _rewrite_link_groups_for_deleted(scene, [_link_ref("guide", guide_id) for guide_id in replaced_ids])
    return next_guide


def _apply_update_guide(scene: Scene, frame_index: int, fields: dict, expected: dict | None = None) -> GuideFrame:
    _require_lane_unlocked(scene, "guide")
    guide = _find_guide(scene, frame_index)
    _validate_guide_identity(guide, expected)
    if "strength" in fields:
        guide.strength = float(fields["strength"])
    if "muted" in fields:
        guide.muted = bool(fields["muted"])
    if "asset_id" in fields:
        guide.asset_id = str(fields["asset_id"] or "")
    if "source" in fields:
        guide.source = str(fields["source"] or "asset")
    if "fit_mode" in fields:
        guide.fit_mode = _validated_fit_mode(fields["fit_mode"])
    if "crop_position" in fields:
        guide.crop_position = _validated_crop_position(fields["crop_position"])
    return guide


def _apply_delete_guide(scene: Scene, frame_index: int, expected: dict | None = None) -> None:
    _require_lane_unlocked(scene, "guide")
    guide = _find_guide(scene, frame_index)
    _validate_guide_identity(guide, expected)
    scene.guide_frames = [current for current in scene.guide_frames if current.frame_index != frame_index]
    _rewrite_link_groups_for_deleted(scene, [_link_ref("guide", getattr(guide, "guide_id", ""))])


def _apply_create_guide(scene: Scene, fields: dict) -> GuideFrame:
    _require_lane_unlocked(scene, "guide")
    guide = GuideFrame(
        guide_id=str(fields.get("guide_id", "") or uuid.uuid4().hex[:8]),
        frame_index=_mutation_int(fields.get("frame_index", 0), "frame_index", 0),
        asset_id=str(fields.get("asset_id", "") or ""),
        source=str(fields.get("source", "asset") or "asset"),
        strength=_mutation_float(fields.get("strength", 1.0), "strength", 1.0),
        muted=bool(fields.get("muted", False)),
        fit_mode=_validated_fit_mode(fields["fit_mode"]) if "fit_mode" in fields else DEFAULT_FIT_MODE,
        crop_position=_validated_crop_position(fields["crop_position"]) if "crop_position" in fields else DEFAULT_CROP_POSITION,
    )
    replaced_ids = [
        getattr(current, "guide_id", "")
        for current in scene.guide_frames
        if current.frame_index == guide.frame_index
    ]
    scene.guide_frames = [current for current in scene.guide_frames if current.frame_index != guide.frame_index]
    scene.guide_frames.append(guide)
    scene.guide_frames.sort(key=lambda g: g.frame_index)
    if replaced_ids:
        _rewrite_link_groups_for_deleted(scene, [_link_ref("guide", guide_id) for guide_id in replaced_ids])
    return guide


def _apply_update_prompt_section(scene: Scene, index: int, fields: dict, expected: dict | None = None) -> PromptSection:
    _require_lane_unlocked(scene, "prompt")
    section = _find_prompt_section(scene, index)
    _validate_prompt_identity(section, expected)
    range_changed = "start_frame" in fields or "end_frame" in fields
    new_start = int(fields["start_frame"]) if "start_frame" in fields else section.start_frame
    new_end = int(fields["end_frame"]) if "end_frame" in fields else section.end_frame
    if range_changed:
        # Text-only updates skip the overlap check so legacy stored overlaps
        # stay editable; only a range change may not create a NEW overlap.
        _require_no_prompt_overlap(scene, new_start, new_end, ignore=section)
    section.start_frame = new_start
    section.end_frame = new_end
    raw_channels = fields.get("channels")
    if isinstance(raw_channels, dict):
        section.channels = prompt_payload.normalize_channels(raw_channels)
    elif "prompt" in fields:
        section.prompt = str(fields["prompt"])
    if "muted" in fields:
        section.muted = bool(fields["muted"])
    scene.prompt_sections.sort(key=lambda s: s.start_frame)
    return section


def _apply_delete_prompt_section(scene: Scene, index: int, expected: dict | None = None) -> None:
    _require_lane_unlocked(scene, "prompt")
    section = _find_prompt_section(scene, index)
    _validate_prompt_identity(section, expected)
    prompt_id = getattr(section, "prompt_id", "")
    scene.prompt_sections.pop(index)
    _rewrite_link_groups_for_deleted(scene, [_link_ref("prompt", prompt_id)])


def _apply_create_prompt_section(scene: Scene, fields: dict) -> PromptSection:
    _require_lane_unlocked(scene, "prompt")
    raw_channels = fields.get("channels")
    start_frame = _mutation_int(fields.get("start_frame", 0), "start_frame", 0)
    end_frame = _mutation_int(fields.get("end_frame", 0), "end_frame", 0)
    _require_no_prompt_overlap(scene, start_frame, end_frame)
    section = PromptSection(
        start_frame=start_frame,
        end_frame=end_frame,
        prompt=str(fields.get("prompt", "") or ""),
        channels=raw_channels if isinstance(raw_channels, dict) else None,
        muted=bool(fields.get("muted", False)),
    )
    if fields.get("prompt_id"):
        section.prompt_id = str(fields.get("prompt_id"))
    scene.prompt_sections.append(section)
    scene.prompt_sections.sort(key=lambda s: s.start_frame)
    return section


def _apply_swap_prompt_sections(scene: Scene, op: dict) -> tuple:
    """Atomically exchange two sections' ranges (threshold-swap commit).

    A swap cannot be two update ops: sections are index-keyed, the array
    re-sorts after every apply, and the overlap check would reject the
    intermediate state. The frontend sends the exact previewed final ranges
    for both sections (mirrors the clip-swap commit rule); only the final
    state is validated.
    """
    _require_lane_unlocked(scene, "prompt")
    index_a = _mutation_int(op.get("index_a"), "index_a")
    index_b = _mutation_int(op.get("index_b"), "index_b")
    if index_a == index_b:
        _mutation_error("swap_prompt_sections requires two distinct sections", 400)
    section_a = _find_prompt_section(scene, index_a)
    section_b = _find_prompt_section(scene, index_b)
    _validate_prompt_identity(section_a, op.get("expected_a"))
    _validate_prompt_identity(section_b, op.get("expected_b"))

    fields_a = op.get("fields_a") if isinstance(op.get("fields_a"), dict) else {}
    fields_b = op.get("fields_b") if isinstance(op.get("fields_b"), dict) else {}
    # Default (no explicit fields): exchange the two ranges verbatim.
    a_start = int(fields_a.get("start_frame", section_b.start_frame))
    a_end = int(fields_a.get("end_frame", section_b.end_frame))
    b_start = int(fields_b.get("start_frame", section_a.start_frame))
    b_end = int(fields_b.get("end_frame", section_a.end_frame))

    if a_start < b_end and a_end > b_start:
        _mutation_error("Prompt sections cannot overlap", 409, "prompt_overlap")
    _require_no_prompt_overlap(scene, a_start, a_end, ignore=[section_a, section_b])
    _require_no_prompt_overlap(scene, b_start, b_end, ignore=[section_a, section_b])

    section_a.start_frame, section_a.end_frame = a_start, a_end
    section_b.start_frame, section_b.end_frame = b_start, b_end
    scene.prompt_sections.sort(key=lambda s: s.start_frame)
    return section_a, section_b


def _apply_scene_mutation_operation(project: TimelineProject, scene: Scene, op: dict) -> dict:
    if not isinstance(op, dict):
        _mutation_error("Mutation operation must be an object", 400)
    op_type = str(op.get("type", ""))
    if op_type == "update_scene_fields":
        _apply_scene_fields(project, scene, op.get("fields", {}))
        return {"type": op_type}
    if op_type == "update_lane_configs":
        _apply_lane_configs(scene, op.get("fields", {}))
        return {"type": op_type}
    if op_type == "update_lane_config":
        return _apply_lane_config(scene, op)
    if op_type == "set_lane_count":
        lane_type = str(op.get("lane_type", ""))
        _set_scene_lane_count(scene, lane_type, max(1, _mutation_int(op.get("count"), "count")))
        return {"type": op_type, "lane_type": lane_type}
    if op_type == "remove_lane":
        _remove_media_lane(
            scene,
            str(op.get("lane_type", "")),
            _mutation_int(op.get("lane_index"), "lane_index"),
            str(op.get("item_policy", "require_empty")),
            op.get("target_lane"),
        )
        return {"type": op_type}
    if op_type == "consolidate_items":
        return _consolidate_media_items(scene, op)
    if op_type == "create_link_group":
        group = _add_link_group(scene, op.get("items", []), str(op.get("group_id", "") or ""))
        return {"type": op_type, "group_id": group["group_id"], "count": len(group["items"])}
    if op_type == "unlink_items":
        refs = [_item_ref_from_selection(scene, item) for item in op.get("items", []) or []]
        if op.get("entire_group"):
            refs = _expand_linked_refs(scene, refs, True)
        _unlink_refs(scene, refs)
        _prune_linked_item_groups(scene)
        return {"type": op_type, "count": len(refs)}
    if op_type == "delete_link_group":
        group_id = str(op.get("group_id", "") or "")
        scene.linked_item_groups = [
            group for group in getattr(scene, "linked_item_groups", []) or []
            if str(group.get("group_id", "") or "") != group_id
        ]
        return {"type": op_type, "group_id": group_id}
    if op_type == "update_clip":
        if op.get("apply_linked"):
            _apply_linked_bounds_update(
                project,
                scene,
                _link_ref("clip", str(op.get("clip_id", ""))),
                op.get("fields", {}),
                bool(op.get("validate_lane_collision", False)),
            )
            return {"type": op_type, "clip_id": str(op.get("clip_id", "")), "linked": True}
        clip = _apply_update_clip(
            project,
            scene,
            str(op.get("clip_id", "")),
            op.get("fields", {}),
            bool(op.get("validate_lane_collision", False)),
        )
        return {"type": op_type, "clip_id": clip.clip_id}
    if op_type == "replace_clip_source":
        clip = _apply_replace_clip_source(
            project,
            scene,
            str(op.get("clip_id", "")),
            str(op.get("asset_id", "")),
        )
        return {"type": op_type, "clip_id": clip.clip_id, "asset_id": str(op.get("asset_id", ""))}
    if op_type == "delete_clip":
        if op.get("apply_linked"):
            _apply_delete_link_refs(scene, [_link_ref("clip", str(op.get("clip_id", "")))], bool(op.get("preserve_lane", False)))
            return {"type": op_type, "clip_id": str(op.get("clip_id", "")), "linked": True}
        _delete_clip(scene, str(op.get("clip_id", "")), bool(op.get("preserve_lane", False)))
        return {"type": op_type, "clip_id": str(op.get("clip_id", ""))}
    if op_type == "update_audio_track":
        if op.get("apply_linked"):
            _apply_linked_bounds_update(
                project,
                scene,
                _link_ref("audio", str(op.get("track_id", ""))),
                op.get("fields", {}),
                bool(op.get("validate_lane_collision", False)),
            )
            return {"type": op_type, "track_id": str(op.get("track_id", "")), "linked": True}
        track = _apply_update_audio_track(
            scene,
            str(op.get("track_id", "")),
            op.get("fields", {}),
            bool(op.get("validate_lane_collision", False)),
        )
        return {"type": op_type, "track_id": track.track_id}
    if op_type == "replace_audio_source":
        track = _apply_replace_audio_source(
            project,
            scene,
            str(op.get("track_id", "")),
            str(op.get("asset_id", "")),
        )
        return {"type": op_type, "track_id": track.track_id, "asset_id": str(op.get("asset_id", ""))}
    if op_type == "delete_audio_track":
        if op.get("apply_linked"):
            _apply_delete_link_refs(scene, [_link_ref("audio", str(op.get("track_id", "")))], bool(op.get("preserve_lane", False)))
            return {"type": op_type, "track_id": str(op.get("track_id", "")), "linked": True}
        _delete_audio_track(scene, str(op.get("track_id", "")), bool(op.get("preserve_lane", False)))
        return {"type": op_type, "track_id": str(op.get("track_id", ""))}
    if op_type == "bulk_delete_items":
        _apply_bulk_delete_items(
            scene,
            op.get("items", []),
            bool(op.get("preserve_lanes", False)),
            bool(op.get("apply_linked", False)),
        )
        return {"type": op_type, "count": len(op.get("items", []) or [])}
    if op_type == "split_clip":
        result = _apply_split_linked(
            scene,
            _link_ref("clip", str(op.get("clip_id", ""))),
            op.get("frame", 0),
            bool(op.get("apply_linked", False)),
        )
        return result
    if op_type == "split_audio_track":
        result = _apply_split_linked(
            scene,
            _link_ref("audio", str(op.get("track_id", ""))),
            op.get("frame", 0),
            bool(op.get("apply_linked", False)),
        )
        return result
    if op_type == "move_guide":
        if op.get("apply_linked"):
            guide = _find_guide(scene, _mutation_int(op.get("from_frame_index"), "from_frame_index"))
            _validate_guide_identity(guide, op.get("expected"))
            _apply_linked_bounds_update(
                project,
                scene,
                _link_ref("guide", getattr(guide, "guide_id", "")),
                {"frame_index": _mutation_int(op.get("to_frame_index"), "to_frame_index")},
            )
            return {"type": op_type, "frame_index": int(getattr(guide, "frame_index", 0)), "linked": True}
        guide = _apply_move_guide(scene, op)
        return {"type": op_type, "frame_index": guide.frame_index}
    if op_type == "update_guide":
        frame_index = _mutation_int(op.get("frame_index"), "frame_index")
        if op.get("apply_linked"):
            guide = _find_guide(scene, frame_index)
            _validate_guide_identity(guide, op.get("expected"))
            _apply_linked_bounds_update(
                project,
                scene,
                _link_ref("guide", getattr(guide, "guide_id", "")),
                op.get("fields", {}),
            )
            return {"type": op_type, "frame_index": frame_index, "linked": True}
        guide = _apply_update_guide(scene, frame_index, op.get("fields", {}), op.get("expected"))
        return {"type": op_type, "frame_index": guide.frame_index}
    if op_type == "delete_guide":
        frame_index = _mutation_int(op.get("frame_index"), "frame_index")
        if op.get("apply_linked"):
            guide = _find_guide(scene, frame_index)
            _validate_guide_identity(guide, op.get("expected"))
            _apply_delete_link_refs(scene, [_link_ref("guide", getattr(guide, "guide_id", ""))], True)
            return {"type": op_type, "frame_index": frame_index, "linked": True}
        _apply_delete_guide(scene, frame_index, op.get("expected"))
        return {"type": op_type, "frame_index": frame_index}
    if op_type == "create_guide":
        guide = _apply_create_guide(scene, op.get("fields", {}))
        return {"type": op_type, "frame_index": guide.frame_index}
    if op_type == "update_prompt_section":
        index = _mutation_int(op.get("index"), "index")
        if op.get("apply_linked"):
            section = _find_prompt_section(scene, index)
            _validate_prompt_identity(section, op.get("expected"))
            _apply_linked_bounds_update(
                project,
                scene,
                _link_ref("prompt", getattr(section, "prompt_id", "")),
                op.get("fields", {}),
            )
            return {"type": op_type, "index": index, "linked": True}
        section = _apply_update_prompt_section(scene, index, op.get("fields", {}), op.get("expected"))
        return {"type": op_type, "index": index, "start_frame": section.start_frame, "end_frame": section.end_frame}
    if op_type == "delete_prompt_section":
        index = _mutation_int(op.get("index"), "index")
        if op.get("apply_linked"):
            section = _find_prompt_section(scene, index)
            _validate_prompt_identity(section, op.get("expected"))
            _apply_delete_link_refs(scene, [_link_ref("prompt", getattr(section, "prompt_id", ""))], True)
            return {"type": op_type, "index": index, "linked": True}
        _apply_delete_prompt_section(scene, index, op.get("expected"))
        return {"type": op_type, "index": index}
    if op_type == "split_prompt_section":
        index = _mutation_int(op.get("index"), "index")
        section = _find_prompt_section(scene, index)
        _validate_prompt_identity(section, op.get("expected"))
        result = _apply_split_linked(
            scene,
            _link_ref("prompt", getattr(section, "prompt_id", "")),
            op.get("frame", 0),
            bool(op.get("apply_linked", False)),
        )
        return result
    if op_type == "create_prompt_section":
        section = _apply_create_prompt_section(scene, op.get("fields", {}))
        return {"type": op_type, "start_frame": section.start_frame, "end_frame": section.end_frame}
    if op_type == "swap_prompt_sections":
        section_a, section_b = _apply_swap_prompt_sections(scene, op)
        return {
            "type": op_type,
            "a": {"start_frame": section_a.start_frame, "end_frame": section_a.end_frame},
            "b": {"start_frame": section_b.start_frame, "end_frame": section_b.end_frame},
        }
    _mutation_error(f"Unsupported mutation operation: {op_type}", 400, "unsupported_project_mutation")


def _apply_scene_mutations_sync(request: web.Request, scene_id: str, operations: list) -> tuple[TimelineProject, dict]:
    project = _load_project_from_request(request)
    scene = project.get_scene(scene_id)
    if not scene:
        _mutation_error(f"Scene not found: {scene_id}", 404, "item_not_found")

    results = [
        _apply_scene_mutation_operation(project, scene, operation)
        for operation in operations
    ]
    _validate_single_driver_per_lane(scene)

    save_project(project)
    return project, {
        "status": "ok",
        "scene_id": scene_id,
        "operation_count": len(operations),
        "results": results,
        "scene": scene.to_dict(),
    }


def _queue_job_from_body(body: dict) -> GenerationJob:
    raw_params = body.get("params", {}) or {}
    params = dict(raw_params) if isinstance(raw_params, dict) else {}
    if any(field in body for field in (
        "pre_context_frames",
        "post_context_frames",
        "guide_frame_snapshots",
        "driver_clip_snapshots",
        "driver_lane_count",
        "driver_lane_configs",
        "prompt_sections",
        "scene_prompt",
        "scene_width",
        "scene_height",
        "scene_fps",
        "template_id",
        "mask_pre_offset",
        "mask_post_offset",
    )):
        params["snapshot_version"] = 1

    raw_take_placement_mode = body.get("take_placement_mode", "trimmed")
    take_placement_mode = raw_take_placement_mode if raw_take_placement_mode in ("trimmed", "untrimmed") else "trimmed"
    # take_placement_linked / take_placement_muted are deliberately NOT read from
    # the body: they are live editing preferences resolved from the editor widgets
    # at execution time, never frozen into the job (user decision 2026-06-11).

    raw_frame_constraint = body.get("frame_constraint")
    frame_constraint = raw_frame_constraint if isinstance(raw_frame_constraint, dict) and raw_frame_constraint else None

    return GenerationJob(
        scene_id=body.get("scene_id", ""),
        scene_name=body.get("scene_name", ""),
        selection_start=int(body.get("selection_start", 0)),
        selection_end=int(body.get("selection_end", 0)),
        batch_id=str(body.get("batch_id", "") or ""),
        batch_total=int(body.get("batch_total", 0)),
        batch_index=int(body.get("batch_index", 0)),
        prompt=body.get("prompt", ""),
        scene_prompt=str(body.get("scene_prompt", "") or ""),
        context_frames=int(body.get("context_frames", 0)),
        pre_context_frames=int(body.get("pre_context_frames", 0)),
        post_context_frames=int(body.get("post_context_frames", 0)),
        mask_pre_offset=int(body.get("mask_pre_offset", 0)),
        mask_post_offset=int(body.get("mask_post_offset", 0)),
        guide_frame_snapshots=list(body.get("guide_frame_snapshots", []) or []),
        driver_clip_snapshots=list(body.get("driver_clip_snapshots", []) or []),
        driver_lane_count=max(1, int(body.get("driver_lane_count", 1) or 1)),
        driver_lane_configs=list(body.get("driver_lane_configs", []) or []),
        prompt_sections=list(body.get("prompt_sections", []) or []),
        scene_width=int(body.get("scene_width", 0)),
        scene_height=int(body.get("scene_height", 0)),
        scene_fps=float(body.get("scene_fps", 0.0) or 0.0),
        template_id=str(body.get("template_id", "free") or "free"),
        frame_constraint=frame_constraint,
        take_placement_mode=take_placement_mode,
        params=params,
    )


PROMPT_HISTORY_CAP = 200  # conscious hard-code; revisit if projects bloat


def _compose_frozen_job_prompt(project: TimelineProject, job: GenerationJob) -> None:
    """Server-side frozen-prompt compose at enqueue (single source of truth).

    For snapshot jobs (snapshot_version > 0) the client-sent `prompt` is a
    best-effort display value only — override it with the authoritative
    multi-segment compose over the job's own frozen fields. Uses ONLY job
    fields + project metadata (no scene lookup; the frozen envelope is the
    authority). Accepted window drift: this is the RAW context window, while
    execution grid-snap can extend it — a section starting wholly inside the
    snap extension is equally absent from the frozen prompt_sections, so the
    relay payload agrees with the string.
    """
    params = getattr(job, "params", {}) or {}
    if not isinstance(params, dict):
        return
    try:
        snapshot_version = int(params.get("snapshot_version", 0) or 0)
    except (TypeError, ValueError):
        snapshot_version = 0
    if snapshot_version <= 0:
        return
    metadata = getattr(project, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    labels_on = params.get(
        "prompt_channel_labels",
        metadata.get("prompt_channel_labels", False),
    ) is True
    delimiter = str(metadata.get("prompt_section_delimiter",
                                 prompt_payload.DEFAULT_SECTION_DELIMITER) or "")
    params["prompt_section_delimiter"] = delimiter  # frozen for reproducibility
    try:
        threshold = float(params.get("prompt_frame_threshold",
                                     metadata.get("prompt_frame_threshold", 10.0)) or 0.0)
    except (TypeError, ValueError):
        threshold = 10.0
    params["prompt_frame_threshold"] = threshold  # frozen for reproducibility
    job.params = params
    window_start = max(0, int(getattr(job, "selection_start", 0) or 0)
                       - int(getattr(job, "pre_context_frames", 0) or 0))
    window_end = (int(getattr(job, "selection_end", 0) or 0)
                  + int(getattr(job, "post_context_frames", 0) or 0))
    job.prompt = prompt_payload.compose_range_prompt(
        getattr(job, "scene_prompt", "") or "",
        getattr(job, "prompt_sections", []) or [],
        window_start, window_end,
        labels_on=labels_on, delimiter=delimiter,
        boundary_threshold_pct=threshold,
    )


def _freeze_guide_collision_param(project: TimelineProject, job: GenerationJob) -> None:
    """Freeze the render-affecting project toggle into snapshot jobs."""
    params = getattr(job, "params", {}) or {}
    if not isinstance(params, dict):
        params = {}
    try:
        snapshot_version = int(params.get("snapshot_version", 0) or 0)
    except (TypeError, ValueError):
        snapshot_version = 0
    if snapshot_version <= 0:
        return
    metadata = getattr(project, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    params["guide_collision_auto_offset"] = metadata.get("guide_collision_auto_offset", True) is not False
    job.params = params


def _queue_guide_collision_prediction(project: TimelineProject, job: GenerationJob) -> None:
    """Attach the backend-authoritative enqueue collision prediction."""
    params = getattr(job, "params", {}) or {}
    if not isinstance(params, dict):
        return
    try:
        snapshot_version = int(params.get("snapshot_version", 0) or 0)
    except (TypeError, ValueError):
        snapshot_version = 0
    if snapshot_version <= 0:
        return
    scene = project.get_scene(getattr(job, "scene_id", ""))
    if scene is None:
        return
    window = resolve_execution_window(
        scene_duration=getattr(scene, "duration_frames", 0),
        selection_start=getattr(job, "selection_start", 0),
        selection_end=getattr(job, "selection_end", 0),
        pre_context_frames=getattr(job, "pre_context_frames", 0),
        post_context_frames=getattr(job, "post_context_frames", 0),
        mask_pre_offset=getattr(job, "mask_pre_offset", 0),
        mask_post_offset=getattr(job, "mask_post_offset", 0),
        frame_constraint=getattr(job, "frame_constraint", None),
    )
    render_start = window["render_start"]
    render_end = window["render_end"]

    guides = []
    seen_ids = set()
    for index, raw in enumerate(getattr(job, "guide_frame_snapshots", []) or []):
        if not isinstance(raw, dict):
            continue
        idx = int(raw.get("frame_index", 0) or 0)
        if idx == -1:
            idx = max(0, int(getattr(scene, "duration_frames", 0) or 0) - 1)
        if not (render_start <= idx < render_end):
            continue
        asset = project.get_asset(str(raw.get("asset_id") or ""))
        if asset is None:
            continue
        path = resolve_existing_project_path(
            project, asset.path, purpose="queue guide collision prediction", log=False
        )
        if not path or not os.path.isfile(path):
            continue
        guide_id = str(raw.get("guide_id") or f"legacy-guide-{index}")
        if guide_id in seen_ids:
            guide_id = f"{guide_id}#{index}"
        seen_ids.add(guide_id)
        guides.append({
            "guide_id": guide_id,
            "bridge_override_key": f"{raw.get('asset_id', '')}:{idx}",
            "local_idx": idx - render_start,
        })

    drivers = []
    for raw in getattr(job, "driver_clip_snapshots", []) or []:
        if not isinstance(raw, dict) or str(raw.get("role") or "render") != "motion_driver":
            continue
        overlap_start = max(int(raw.get("timeline_start_frame", 0) or 0), render_start)
        overlap_end = min(int(raw.get("timeline_end_frame", 0) or 0), render_end)
        if overlap_end <= overlap_start:
            continue
        asset_lookup = getattr(project, "asset_for_source_path", None)
        asset = asset_lookup(str(raw.get("source_path") or "")) if callable(asset_lookup) else None
        if asset is None or getattr(asset, "asset_type", "") != "video":
            continue
        path = resolve_existing_project_path(
            project, asset.path, purpose="queue driver collision prediction", log=False
        )
        if not path or not os.path.isfile(path):
            continue
        drivers.append({
            "clip_id": str(raw.get("clip_id") or ""),
            "lane_index": int(raw.get("track_index", 0) or 0),
            "local_idx": overlap_start - render_start,
            "pixel_len": overlap_end - overlap_start,
        })

    prediction = resolve_guide_collisions(
        guides=guides,
        drivers=drivers,
        frame_count=window["frame_count"],
        frame_constraint=getattr(job, "frame_constraint", None),
        auto_offset_enabled=params.get("guide_collision_auto_offset") is not False,
    )
    # Queue rows need the decision and suggested guide moves, not every driver
    # coordinate. Keep durable snapshot params compact; execution recomputes the
    # full authoritative manifest from the frozen envelope.
    prediction.pop("driver_coords", None)
    prediction.update({
        "schema": "sonder_guide_injection_v1",
        "auto_offset_enabled": params.get("guide_collision_auto_offset") is not False,
        "frame_count": window["frame_count"],
        "execution_window": window,
    })
    params["guide_collision_prediction"] = prediction
    job.params = params


def _record_prompt_history(project: TimelineProject, jobs: list) -> None:
    """Capture executed prompt material at enqueue (Prompt Saver).

    Runs inside the same load/save as the queue append (no extra save). One
    entry per distinct prompt content: sha256 over global + section
    ranges/channels; duplicates bump `ts` instead of appending. Capped to the
    newest PROMPT_HISTORY_CAP entries. Live non-queue runs are deliberately
    NOT captured — execution-time project writes would fight the
    generated-commit path.
    """
    import hashlib
    from datetime import datetime as _dt

    if not isinstance(getattr(project, "metadata", None), dict):
        return
    history = project.metadata.get("prompt_history")
    if not isinstance(history, list):
        history = []

    for job in jobs or []:
        sections = [
            {
                "start_frame": int(s.get("start_frame", 0)),
                "end_frame": int(s.get("end_frame", 0)),
                "channels": prompt_payload.normalize_channels(
                    s.get("channels"), legacy_prompt=s.get("prompt", "")
                ),
            }
            for s in (getattr(job, "prompt_sections", []) or [])
            if isinstance(s, dict)
        ]
        global_text = str(getattr(job, "scene_prompt", "") or "")
        if not global_text and not sections:
            continue
        digest = hashlib.sha256(json.dumps(
            {"global": global_text, "sections": sections}, sort_keys=True
        ).encode("utf-8")).hexdigest()[:16]
        timestamp = _dt.now().isoformat()
        existing = next((entry for entry in history
                         if isinstance(entry, dict) and entry.get("hash") == digest), None)
        if existing is not None:
            existing["ts"] = timestamp
            continue
        history.append({
            "hash": digest,
            "ts": timestamp,
            "scene_id": str(getattr(job, "scene_id", "") or ""),
            "window": [int(getattr(job, "selection_start", 0) or 0),
                       int(getattr(job, "selection_end", 0) or 0)],
            "global": global_text,
            "sections": sections,
        })

    history.sort(key=lambda entry: str(entry.get("ts", "")) if isinstance(entry, dict) else "")
    if len(history) > PROMPT_HISTORY_CAP:
        history = history[-PROMPT_HISTORY_CAP:]
    project.metadata["prompt_history"] = history


def _queue_payload(project: TimelineProject) -> list[dict]:
    return [job.to_dict() for job in project.generation_queue]


def _load_project_after_queue_conflict(
    exc: ProjectVersionConflict,
    fallback_project: TimelineProject | None = None,
) -> TimelineProject:
    project_dir = str(getattr(exc, "project_dir", "") or "")
    if not project_dir and fallback_project is not None:
        project_dir = str(getattr(fallback_project, "project_dir", "") or "")
    if not project_dir:
        raise exc
    return load_project(project_dir)


def _apply_queue_versioned_sync(request: web.Request, apply_fn, max_attempts: int = 3):
    project: TimelineProject | None = None
    for attempt in range(max_attempts):
        if project is None:
            try:
                project = _load_project_from_request(request)
            except ProjectVersionConflict as exc:
                project = _load_project_after_queue_conflict(exc)
        base_modified_at = str(getattr(project, "modified_at", "") or "")
        changed, payload = apply_fn(project)
        if not changed:
            return project, payload
        try:
            save_project(project, expected_modified_at=base_modified_at)
            return project, payload
        except ProjectVersionConflict as exc:
            if attempt >= max_attempts - 1:
                raise
            project = _load_project_after_queue_conflict(exc, project)
    raise RuntimeError("Queue mutation retry loop exhausted")


def _apply_queue_mutation_operations(project: TimelineProject, operations: list) -> tuple[bool, dict]:
    changed = False
    results = []
    for operation in operations:
        if not isinstance(operation, dict):
            _mutation_error("Queue operation must be an object", 400, "invalid_queue_mutation")
        op_type = str(operation.get("type", "") or "")
        if op_type == "delete_job":
            job_id = str(operation.get("job_id", "") or "")
            if not job_id:
                _mutation_error("delete_job requires job_id", 400, "invalid_queue_mutation")
            before = len(project.generation_queue)
            project.generation_queue = [job for job in project.generation_queue if job.job_id != job_id]
            removed = before - len(project.generation_queue)
            changed = changed or removed > 0
            results.append({"type": op_type, "job_id": job_id, "removed": removed})
        elif op_type == "clear_completed":
            before = len(project.generation_queue)
            project.generation_queue = [
                job for job in project.generation_queue
                if str(getattr(job, "status", "") or "").lower() != "completed"
            ]
            removed = before - len(project.generation_queue)
            changed = changed or removed > 0
            results.append({"type": op_type, "removed": removed})
        elif op_type == "clear_all":
            removed = len(project.generation_queue)
            if removed:
                project.generation_queue.clear()
                changed = True
            results.append({"type": op_type, "removed": removed})
        else:
            _mutation_error(f"Unsupported queue operation: {op_type}", 400, "unsupported_queue_mutation")

    return changed, {
        "status": "ok",
        "operation_count": len(operations),
        "results": results,
        "queue": _queue_payload(project),
    }


def _asset_abspath(project: TimelineProject, asset: Asset) -> str:
    asset_path = getattr(asset, "path", "") or ""
    if not asset_path:
        return ""
    normalized = str(asset_path).replace("\\", "/").strip("/")
    if not normalized.startswith("media/"):
        log_path_quarantine(
            purpose=f"asset source {getattr(asset, 'asset_id', '') or '(unknown)'}",
            path=asset_path,
            root=getattr(project, "project_dir", "") or "",
            reason="asset source outside project media directory",
        )
        return ""
    return project_media_path(
        project,
        normalized,
        purpose=f"asset source {getattr(asset, 'asset_id', '') or '(unknown)'}",
    )


def _asset_missing(project: TimelineProject, asset: Asset) -> bool:
    source_path = _asset_abspath(project, asset)
    return not source_path or not os.path.isfile(source_path)


def _require_asset_media_source(project: TimelineProject, asset: Asset, *, operation: str) -> str:
    source_path = _asset_abspath(project, asset)
    if not source_path:
        asset_id = getattr(asset, "asset_id", "") or "(unknown)"
        raise ValueError(f"{operation} requires asset source under project media: {asset_id}")
    return source_path


def _require_asset_media_sources(project: TimelineProject, assets, *, operation: str) -> None:
    for asset in assets:
        _require_asset_media_source(project, asset, operation=operation)


def _normalize_project_relpath(path: str) -> str:
    return str(path or "").replace("\\", "/").strip("/")


def _media_probe_signature_from_stat(stat_result) -> str:
    try:
        return f"{stat_result.st_size}:{stat_result.st_mtime_ns}"
    except Exception:
        return ""


def _snapshot_files_under(root_dir: str, rel_prefix: str = "") -> dict[str, dict]:
    trust_links = external_links.is_enabled()
    root_dir = os.path.abspath(root_dir) if trust_links else os.path.realpath(root_dir)
    if not os.path.isdir(root_dir):
        return {}
    root_anchor = root_dir

    root_rel = _normalize_project_relpath(rel_prefix)
    snapshot: dict[str, dict] = {}
    stack = [(root_dir, root_rel)]
    visited_real_dirs: set[str] = set()
    while stack:
        current_dir, current_rel = stack.pop()
        if trust_links:
            try:
                current_real = os.path.normcase(os.path.realpath(current_dir))
            except OSError:
                continue
            if current_real in visited_real_dirs:
                continue
            visited_real_dirs.add(current_real)
        try:
            with os.scandir(current_dir) as scan:
                for entry in scan:
                    rel_path = _normalize_project_relpath(os.path.join(current_rel, entry.name))
                    try:
                        # Direct browser uploads stage beside their final destination so
                        # the publish rename stays on one physical volume. This reserved
                        # directory is transactional state, never project media.
                        if entry.name == UPLOAD_STAGING_DIRNAME:
                            continue
                        try:
                            if entry.is_symlink() and not trust_links:
                                log_path_quarantine(
                                    purpose="media snapshot",
                                    path=rel_path,
                                    root=root_anchor,
                                    reason="symlink skipped",
                                )
                                continue
                        except (AttributeError, OSError):
                            continue
                        if entry.is_dir(follow_symlinks=trust_links):
                            if not _security_path_within(root_anchor, entry.path):
                                log_path_quarantine(
                                    purpose="media snapshot",
                                    path=rel_path,
                                    root=root_anchor,
                                    reason="directory escapes root",
                                )
                                continue
                            if trust_links:
                                try:
                                    child_real = os.path.normcase(os.path.realpath(entry.path))
                                except OSError:
                                    continue
                                if child_real in visited_real_dirs:
                                    logger.debug("Skipping already-visited linked media directory: %s", entry.path)
                                    continue
                            stack.append((entry.path, rel_path))
                            continue
                        if not entry.is_file(follow_symlinks=trust_links):
                            continue
                        if not _security_path_within(root_anchor, entry.path):
                            log_path_quarantine(
                                purpose="media snapshot",
                                path=rel_path,
                                root=root_anchor,
                                reason="file escapes root",
                            )
                            continue
                        stat_result = entry.stat(follow_symlinks=trust_links)
                    except OSError:
                        continue
                    snapshot[rel_path] = {
                        "path": entry.path,
                        "size": int(getattr(stat_result, "st_size", 0) or 0),
                        "signature": _media_probe_signature_from_stat(stat_result),
                    }
        except OSError:
            continue
    return snapshot


def _project_media_snapshot(project: TimelineProject) -> dict[str, dict]:
    media_dir = project_media_root(project, must_exist=True)
    return _snapshot_files_under(media_dir, "media") if media_dir else {}


def _project_thumbnail_snapshot(project: TimelineProject) -> dict[str, dict]:
    cache_dir = resolve_project_path(project, os.path.join("cache", "thumbnails"), purpose="thumbnail cache root", must_exist=True)
    return _snapshot_files_under(cache_dir) if cache_dir else {}


def _project_asset_for_source_path(project: TimelineProject, source_path: str) -> Asset | None:
    normalized = _normalize_project_relpath(source_path)
    if not normalized.startswith("media/"):
        return None
    direct = project.asset_for_source_path(source_path)
    if direct is not None:
        return direct
    abs_source = project_media_path(project, source_path, purpose="asset lookup", log=False)
    if not abs_source:
        return None
    for asset in getattr(project, "assets", []) or []:
        asset_path = getattr(asset, "path", "") or ""
        if not _normalize_project_relpath(asset_path).startswith("media/"):
            continue
        if _normalize_project_relpath(asset_path) == normalized:
            return asset
        try:
            asset_abs = _asset_abspath(project, asset)
            if asset_abs and abs_source and os.path.normcase(asset_abs) == os.path.normcase(abs_source):
                return asset
        except Exception:
            continue
    return None


def _extract_frame_source(project: TimelineProject, body: dict) -> tuple[str, str]:
    asset_id = str(body.get("asset_id") or "").strip()
    if asset_id:
        try:
            asset_id = _safe_route_token(asset_id, "asset id")
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        asset = project.get_asset(asset_id)
        if not asset:
            raise FileNotFoundError(f"Asset not found: {asset_id}")
    else:
        raw_source_path = str(body.get("source_path") or "")
        if not raw_source_path:
            raise ValueError("asset_id or source_path is required")
        try:
            source_rel_path = normalize_project_relative_path(raw_source_path)
        except PathSecurityError as exc:
            raise ValueError(f"Invalid source_path: {exc}") from exc
        asset = next(
            (
                item for item in getattr(project, "assets", []) or []
                if _normalize_project_relpath(getattr(item, "path", "") or "") == source_rel_path
            ),
            None,
        )
        if not asset:
            raise ValueError("source_path is not a registered project asset")

    if getattr(asset, "asset_type", "") != "video":
        raise ValueError("Frame extraction requires a video asset")
    abs_path = _asset_abspath(project, asset)
    if not abs_path or not os.path.isfile(abs_path):
        raise FileNotFoundError(f"Source file not found: {getattr(asset, 'path', '') or asset_id}")
    return getattr(asset, "path", "") or "", abs_path


def _asset_payload(
    project: TimelineProject,
    asset: Asset,
    *,
    media_snapshot: dict[str, dict] | None = None,
    thumbnail_snapshot: dict[str, dict] | None = None,
) -> dict:
    payload = asset.to_dict()
    source_path = _asset_abspath(project, asset)
    source_rel = _normalize_project_relpath(getattr(asset, "path", "") or "")
    cache_key = _asset_cache_key(project, asset)
    thumb_path = _asset_thumbnail_path(project, asset) if cache_key else ""
    thumb_key = _normalize_project_relpath(f"{cache_key}.png") if cache_key else ""
    payload["has_thumbnail"] = (
        bool(thumb_key) and thumb_key in thumbnail_snapshot
        if thumbnail_snapshot is not None
        else bool(thumb_path) and os.path.isfile(thumb_path)
    )
    if media_snapshot is not None and source_rel.startswith("media/"):
        source_info = media_snapshot.get(source_rel)
        payload["missing"] = source_info is None
        payload["size_bytes"] = int(source_info.get("size", 0)) if source_info else 0
    else:
        payload["missing"] = _asset_missing(project, asset)
        payload["size_bytes"] = _asset_file_size(source_path) if source_path else 0
    payload["trashed_at"] = getattr(asset, "trashed_at", "") or ""
    payload["extension"] = os.path.splitext(getattr(asset, "path", "") or "")[1].lower()
    return payload


def _asset_payloads(project: TimelineProject, assets) -> list[dict]:
    media_snapshot = _project_media_snapshot(project)
    thumbnail_snapshot = _project_thumbnail_snapshot(project)
    return [
        _asset_payload(
            project,
            asset,
            media_snapshot=media_snapshot,
            thumbnail_snapshot=thumbnail_snapshot,
        )
        for asset in assets
    ]


def _parse_metadata_json(value):
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _workflow_from_metadata_tags(tags: dict):
    if not isinstance(tags, dict):
        return None
    editor_export = None
    for key, value in tags.items():
        normalized = str(key or "").lower()
        if normalized == "workflow":
            parsed = _parse_metadata_json(value)
            if isinstance(parsed, dict):
                return parsed
        elif normalized == "editor_export":
            parsed = _parse_metadata_json(value)
            if isinstance(parsed, dict):
                editor_export = parsed
    workflow = editor_export.get("workflow") if isinstance(editor_export, dict) else None
    return workflow if isinstance(workflow, dict) else None


def _extract_png_workflow_metadata(path: str):
    try:
        from PIL import Image

        with Image.open(path) as image:
            return _workflow_from_metadata_tags(image.info or {})
    except Exception as exc:
        logger.debug("PNG workflow metadata extraction failed for %s: %s", path, exc)
        return None


def _ffmetadata_unescape(value: str) -> str:
    result = []
    escaped = False
    replacements = {"n": "\n", "r": "\r"}
    for char in str(value):
        if escaped:
            result.append(replacements.get(char, char))
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            result.append(char)
    if escaped:
        result.append("\\")
    return "".join(result)


def _split_ffmetadata_line(line: str) -> tuple[str, str] | None:
    escaped = False
    for index, char in enumerate(str(line)):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "=":
            return (_ffmetadata_unescape(line[:index]), _ffmetadata_unescape(line[index + 1:]))
    return None


def _parse_ffmetadata_tags(text: str) -> dict:
    tags = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line == ";FFMETADATA1" or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            continue
        pair = _split_ffmetadata_line(raw_line)
        if not pair:
            continue
        key, value = pair
        if key:
            tags[key] = value
    return tags


def _extract_video_workflow_metadata_ffprobe(path: str):
    import subprocess

    result = subprocess.run(
        [
            get_ffprobe_path(),
            "-v",
            "quiet",
            "-show_format",
            "-print_format",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        logger.debug("ffprobe workflow metadata extraction failed for %s: %s", path, (result.stderr or "")[:240])
        return None
    data = json.loads(result.stdout or "{}")
    tags = ((data.get("format") or {}).get("tags") or {})
    return _workflow_from_metadata_tags(tags)


def _extract_video_workflow_metadata_ffmpeg(path: str):
    import subprocess

    result = subprocess.run(
        [
            get_ffmpeg_path(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-f",
            "ffmetadata",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        logger.debug("ffmpeg workflow metadata extraction failed for %s: %s", path, (result.stderr or "")[:240])
        return None
    return _workflow_from_metadata_tags(_parse_ffmetadata_tags(result.stdout or ""))


def _extract_video_workflow_metadata(path: str):
    try:
        workflow = _extract_video_workflow_metadata_ffprobe(path)
        if workflow is not None:
            return workflow
    except Exception as exc:
        logger.debug("ffprobe video workflow metadata extraction failed for %s: %s", path, exc)
    try:
        return _extract_video_workflow_metadata_ffmpeg(path)
    except Exception as exc:
        logger.debug("ffmpeg video workflow metadata extraction failed for %s: %s", path, exc)
    return None


def _extract_embedded_workflow(project: TimelineProject, asset: Asset):
    path = _asset_abspath(project, asset)
    if not path or not os.path.isfile(path):
        return None
    ext = os.path.splitext(path)[1].lower()
    if ext == ".png":
        return _extract_png_workflow_metadata(path)
    if ext in {".mp4", ".m4v", ".mov", ".mkv"}:
        return _extract_video_workflow_metadata(path)
    return None


def _get_audio_duration(filepath: str) -> float:
    """Get audio duration in seconds using the centralized media probe."""
    try:
        duration = float(probe_audio_duration(filepath) or 0.0)
        if duration > 0:
            logger.info("Audio duration: %.2fs (%s)", duration, os.path.basename(filepath))
            return duration
    except Exception as e:
        logger.debug("audio duration probe failed for %s: %s", filepath, e)
    logger.warning("All audio duration methods failed for %s", filepath)
    return 0.0


def _video_has_audio(filepath: str) -> bool:
    """Check if a video file contains an audio stream using the centralized media probe."""
    try:
        return bool(probe_media_has_audio(filepath))
    except Exception as e:
        logger.debug("audio stream probe failed for %s: %s", filepath, e)
        return False

def _get_ffmpeg() -> str:
    return get_ffmpeg_path()


def _read_image_size(image_path: str) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            return img.size
    except Exception:
        try:
            import cv2
            img = cv2.imread(image_path)
            if img is None:
                return None
            h, w = img.shape[:2]
            return w, h
        except Exception:
            return None


def _extract_video_frame_ffmpeg(video_path: str, frame_index: int, output_path: str) -> tuple[int, int] | None:
    try:
        frame_rgb = decode_video_frame(video_path, frame_index)
        if frame_rgb is None:
            return None
        write_png(output_path, frame_rgb)
        h, w = frame_rgb.shape[:2]
        return (w, h)
    except Exception as e:
        logger.warning("ffmpeg frame extraction failed for %s: %s", video_path, e)
        return None


def _extract_audio_from_video(video_path: str, output_path: str) -> bool:
    """Extract audio track from video file as WAV."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Method 1: ffmpeg
    try:
        import subprocess
        ffmpeg = _get_ffmpeg()
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", video_path, "-vn",
             "-acodec", "pcm_s16le", "-ar", "44100", output_path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and os.path.isfile(output_path):
            logger.info("Extracted audio from video via ffmpeg: %s", os.path.basename(video_path))
            return True
        summary = _summarize_ffmpeg_stderr(result.stderr)
        if _ffmpeg_no_audio_stderr(result.stderr):
            logger.debug("No embedded audio stream found in %s: %s", os.path.basename(video_path), summary)
        else:
            _warn_once(
                ("ffmpeg-audio-extract", result.returncode, summary),
                "ffmpeg audio extraction failed for %s (exit %d): %s",
                os.path.basename(video_path),
                result.returncode,
                summary,
            )
    except Exception as e:
        _warn_once(
            ("ffmpeg-audio-extract-exception", type(e).__name__, str(e)),
            "ffmpeg audio extraction failed for %s: %s",
            os.path.basename(video_path),
            e,
        )

    return False


def _prepare_video_audio_asset(project: TimelineProject, asset: Asset) -> Asset | None:
    """Extract a video asset's embedded audio into a derived audio asset.

    Shared by dual-drop clip creation and audio-only video drops. Reuses the
    derived ``media/{asset_id}_audio.wav`` path so repeated drops of the same
    video dedupe to one audio asset. Returns the audio Asset, or None when the
    video has no usable audio. Blocking (ffmpeg/ffprobe/thumbnail) — call via
    asyncio.to_thread from route handlers.
    """
    video_path = _asset_abspath(project, asset)
    asset_key = _asset_storage_key(project, asset, purpose="derived audio asset id")
    if not asset_key:
        return None
    audio_filename = f"{asset_key}_audio.wav"
    audio_rel_path = os.path.join("media", audio_filename)
    audio_abs_path = resolve_project_path(project, audio_rel_path, purpose="derived audio path")
    if not audio_abs_path:
        return None
    audio_rel_norm = _normalize_project_relpath(audio_rel_path)
    existing_audio_asset = next(
        (
            a for a in project.assets
            if _normalize_project_relpath(getattr(a, "path", "") or "") == audio_rel_norm
        ),
        None,
    )

    extracted = True
    if not os.path.isfile(audio_abs_path):
        if not video_path or not os.path.isfile(video_path):
            return None
        extracted = _extract_audio_from_video(video_path, audio_abs_path)

    audio_dur = 0.0
    if (
        extracted
        and os.path.isfile(audio_abs_path)
        and os.path.getsize(audio_abs_path) > 1024
    ):
        audio_dur = _get_audio_duration(audio_abs_path)

    if audio_dur > 0:
        audio_asset = existing_audio_asset
        if audio_asset:
            if audio_asset.duration_sec != audio_dur:
                audio_asset.duration_sec = audio_dur
            audio_asset.duration_checked = True
            audio_asset.media_probe_signature = _media_probe_signature(audio_abs_path)
        else:
            audio_asset = Asset(
                name=f"{asset.name} (audio)",
                asset_type="audio",
                path=audio_rel_path,
                duration_sec=audio_dur,
                duration_checked=True,
                media_probe_signature=_media_probe_signature(audio_abs_path),
            )
            project.add_asset(audio_asset)
            thumb_path = _asset_thumbnail_path(project, audio_asset)
            if thumb_path:
                ensure_thumbnail("audio", audio_abs_path, thumb_path)
        return audio_asset

    # Empty/failed extraction: clean up the orphan unless an asset references it
    if os.path.isfile(audio_abs_path) and not existing_audio_asset:
        try:
            os.remove(audio_abs_path)
        except OSError:
            pass
    return None


def _media_probe_signature(filepath: str) -> str:
    try:
        stat = os.stat(filepath)
        return f"{stat.st_size}:{stat.st_mtime_ns}"
    except OSError:
        return ""


def _sync_media_folder(
    project: TimelineProject,
    trash_retention_days: int = TRASH_RETENTION_DAYS,
    trash_max_size_mb: float | None = None,
    *,
    purge_trashed: bool = True,
) -> bool:
    """Scan media/ folder for files not yet in the asset registry and add them.

    Returns True if any changes were made (new assets discovered or repaired).
    """
    changed = False
    if purge_trashed:
        changed = _purge_expired_trashed_assets(
            project,
            retention_days=trash_retention_days,
            max_size_mb=trash_max_size_mb,
        )
    media_dir = project_media_root(project, must_exist=True)
    if not os.path.isdir(media_dir):
        return changed
    media_snapshot = _project_media_snapshot(project)

    def _mark_probe_state(asset: Asset, signature: str, *, has_audio: bool | None = None, duration: bool | None = None) -> bool:
        probe_changed = False
        if signature and getattr(asset, "media_probe_signature", "") != signature:
            asset.media_probe_signature = signature
            probe_changed = True
        if has_audio is not None and getattr(asset, "has_audio_checked", False) != has_audio:
            asset.has_audio_checked = has_audio
            probe_changed = True
        if duration is not None and getattr(asset, "duration_checked", False) != duration:
            asset.duration_checked = duration
            probe_changed = True
        return probe_changed

    # Build set of known relative paths
    known_paths = {str(a.path or "").replace("\\", "/") for a in project.assets}
    for rel_path, file_info in media_snapshot.items():
        media_child = rel_path[len("media/"):] if rel_path.startswith("media/") else rel_path
        if "/" in media_child:
            continue

        filepath = file_info.get("path") or resolve_project_path(project, rel_path, purpose="media sync file")
        if not filepath:
            continue
        filename = os.path.basename(rel_path)
        # Skip transient work files that node/export code may write into media/ and
        # delete moments later (save_video / export audio mux, export *.tmp output).
        # ffprobing one mid-life races its os.remove → WinError 32 (sharing violation),
        # failing the save node. Deny-list the known temp conventions only — real
        # assets never use these names. Defense-in-depth behind the temp-dir relocation.
        if filename.startswith("_tmp_") or ".tmp." in filename or filename.endswith(".tmp"):
            continue
        if rel_path in known_paths:
            continue

        asset_type, artifact_kind = _classify_asset_for_registration(filename)

        try:
            metadata = _extract_asset_media_metadata(
                filepath,
                asset_type,
                strict=_media_asset_requires_probe(asset_type),
            )
        except MediaProbeError as exc:
            logger.warning("Skipped auto-registering unprobeable media %s: %s", filename, exc)
            continue

        asset = Asset(
            name=filename,
            asset_type=asset_type,
            artifact_kind=artifact_kind,
            path=rel_path,
            width=metadata["width"],
            height=metadata["height"],
            frame_count=metadata["frame_count"],
            fps=metadata["fps"],
            duration_sec=metadata["duration_sec"],
            sample_rate=metadata["sample_rate"],
            has_audio=metadata["has_audio"],
            has_audio_checked=asset_type == "video" and _metadata_checked_for_asset(asset_type, metadata),
            duration_checked=asset_type == "audio" and _metadata_checked_for_asset(asset_type, metadata),
            media_probe_signature=file_info.get("signature") or _media_probe_signature(filepath),
        )
        if asset_type == "video":
            apply_color_metadata(asset, metadata)
        project.add_asset(asset)

        if asset_type in {"video", "image", "audio"}:
            thumb_path = _asset_thumbnail_path(project, asset)
            if thumb_path:
                ensure_thumbnail(asset_type, filepath, thumb_path)

        changed = True
        logger.info("Auto-registered asset: %s (%s)", filename, asset_type)

    # Repair video assets missing audio detection or carrying corrupt metadata
    repaired_video_assets = {}
    for asset in project.assets:
        if asset.asset_type != "video":
            continue
        rel_path = _normalize_project_relpath(getattr(asset, "path", "") or "")
        file_info = media_snapshot.get(rel_path)
        if not file_info:
            continue
        filepath = file_info.get("path") or resolve_project_path(project, rel_path, purpose="video repair file")
        if not filepath:
            continue
        signature = file_info.get("signature") or _media_probe_signature(filepath)
        checked = bool(getattr(asset, "has_audio_checked", False))
        stored_signature = getattr(asset, "media_probe_signature", "") or ""
        signature_changed = bool(stored_signature and signature and stored_signature != signature)
        current_metadata = {
            "width": getattr(asset, "width", 0),
            "height": getattr(asset, "height", 0),
            "frame_count": getattr(asset, "frame_count", 0),
            "fps": getattr(asset, "fps", 0.0),
            "duration_sec": getattr(asset, "duration_sec", 0.0),
        }
        metadata_valid = is_valid_media_metadata(current_metadata, "video")
        if metadata_valid and asset.has_audio and not checked and not stored_signature:
            changed = _mark_probe_state(asset, signature, has_audio=True) or changed
            continue
        if checked and metadata_valid and not signature_changed:
            if signature and not stored_signature:
                changed = _mark_probe_state(asset, signature) or changed
            continue
        try:
            metadata = _extract_asset_media_metadata(filepath, "video", strict=True)
        except MediaProbeError as exc:
            logger.warning("Video metadata repair failed for %s: %s", asset.name, exc)
            continue
        for field in ("width", "height", "frame_count", "fps", "duration_sec", "sample_rate", "has_audio"):
            if getattr(asset, field, None) != metadata[field]:
                setattr(asset, field, metadata[field])
                changed = True
        changed = apply_color_metadata(asset, metadata) or changed
        if metadata.get("has_audio"):
            logger.info("Detected audio in video: %s", asset.name)
        repaired_video_assets[rel_path] = asset
        changed = _mark_probe_state(asset, signature, has_audio=True) or changed

    # One-time color backfill for video assets that predate color probing and
    # were skipped by the repair pass above (valid metadata, unchanged file).
    # Rides the sync save, which is no-bump/no-notify by design — do not add
    # broadcasts here (runaway /assets WS loop, fixed 2026-06-26).
    for asset in project.assets:
        if asset.asset_type != "video" or getattr(asset, "color_probed", False):
            continue
        rel_path = _normalize_project_relpath(getattr(asset, "path", "") or "")
        file_info = media_snapshot.get(rel_path)
        if not file_info:
            continue
        filepath = file_info.get("path") or resolve_project_path(project, rel_path, purpose="color probe file")
        if not filepath:
            continue
        color_metadata = probe_video_color_metadata(filepath)
        if not color_metadata.get("color_probed"):
            continue
        changed = apply_color_metadata(asset, color_metadata) or changed

    # Repair existing audio assets with missing duration
    repaired_assets = {}
    for asset in project.assets:
        if asset.asset_type != "audio":
            continue
        rel_path = _normalize_project_relpath(getattr(asset, "path", "") or "")
        file_info = media_snapshot.get(rel_path)
        if not file_info:
            continue
        filepath = file_info.get("path") or resolve_project_path(project, rel_path, purpose="audio repair file")
        if not filepath:
            continue
        signature = file_info.get("signature") or _media_probe_signature(filepath)
        checked = bool(getattr(asset, "duration_checked", False))
        stored_signature = getattr(asset, "media_probe_signature", "") or ""
        signature_changed = bool(stored_signature and signature and stored_signature != signature)
        duration_valid = _finite_positive_number(getattr(asset, "duration_sec", 0.0))
        if duration_valid and not checked and not stored_signature:
            changed = _mark_probe_state(asset, signature, duration=True) or changed
            continue
        if checked and duration_valid and not signature_changed:
            if signature and not stored_signature:
                changed = _mark_probe_state(asset, signature) or changed
            continue
        dur = _get_audio_duration(filepath)
        if dur and dur > 0:
            asset.duration_sec = dur
            repaired_assets[_normalize_project_relpath(asset.path)] = asset
            changed = True
            logger.info("Repaired audio duration: %.2fs (%s)", dur, asset.name)
            changed = _mark_probe_state(asset, signature, duration=True) or changed
        else:
            if checked:
                asset.duration_checked = False
                changed = True
            if signature and not stored_signature:
                changed = _mark_probe_state(asset, signature) or changed

    # Repair audio tracks with 1-frame duration (caused by previous 0-duration bug)
    if repaired_assets:
        for scene in project.scenes:
            fps = effective_scene_fps(project, scene)
            for track in scene.audio_tracks:
                duration_frames = track.timeline_end_frame - track.timeline_start_frame
                track_source = _normalize_project_relpath(getattr(track, "source_path", "") or "")
                if duration_frames <= 1 and track_source in repaired_assets:
                    new_duration = media_timeline_frames(repaired_assets[track_source], fps)
                    if new_duration > 1:
                        track.timeline_end_frame = track.timeline_start_frame + new_duration
                        track.total_source_frames = max(track.total_source_frames or 0, new_duration)
                        changed = True
                        logger.info("Repaired audio track duration: %d frames (%s)", new_duration, track.source_path)

    if repaired_video_assets:
        for scene in project.scenes:
            for clip in scene.clips:
                clip_source = _normalize_project_relpath(getattr(clip, "source_path", "") or "")
                repaired_asset = repaired_video_assets.get(clip_source)
                if not repaired_asset:
                    continue
                source_frames = media_timeline_frames(repaired_asset, effective_scene_fps(project, scene))
                if source_frames <= 0:
                    continue
                duration_frames = int(getattr(clip, "timeline_end_frame", 0) or 0) - int(getattr(clip, "timeline_start_frame", 0) or 0)
                if duration_frames > 0 and int(getattr(clip, "source_out_frame", 0) or 0) > 0 and int(getattr(clip, "total_source_frames", 0) or 0) > 0:
                    continue
                clip.source_in_frame = max(0, int(getattr(clip, "source_in_frame", 0) or 0))
                clip.source_out_frame = max(clip.source_in_frame + 1, source_frames)
                clip.total_source_frames = max(1, source_frames)
                clip.timeline_end_frame = int(getattr(clip, "timeline_start_frame", 0) or 0) + max(1, source_frames - clip.source_in_frame)
                changed = True
                logger.info("Repaired video clip duration: %d frames (%s)", source_frames, clip.source_path)

    return changed


def _save_versioned_sync_phase(project: TimelineProject, sync_fn, reload_project) -> TimelineProject:
    for _attempt in range(3):
        base_modified_at = str(getattr(project, "modified_at", "") or "")
        changed = sync_fn(project)
        if not changed:
            return project
        try:
            # Discovery-sync is opportunistic GET-side repair, not a user edit:
            # save WITHOUT a version bump or a project_updated broadcast (durable_rules
            # #279). A bump here re-arms the WS→/assets refresh loop (~10/sec churn) AND
            # spuriously 409s in-flight save-node commits (their expected_modified_at goes
            # stale mid-render). Discovered assets re-surface on the next load.
            save_project(project, expected_modified_at=base_modified_at, bump_modified_at=False, notify=False)
            return project
        except ProjectVersionConflict:
            project = reload_project()
    base_modified_at = str(getattr(project, "modified_at", "") or "")
    changed = sync_fn(project)
    if changed:
        save_project(project, expected_modified_at=base_modified_at, bump_modified_at=False, notify=False)
    return project


def _sync_media_folder_versioned(
    project: TimelineProject,
    trash_retention_days: int = TRASH_RETENTION_DAYS,
    trash_max_size_mb: float | None = None,
    *,
    reload_project=None,
) -> TimelineProject:
    if reload_project is None:
        reload_project = lambda: load_project(project.project_dir)

    project = _save_versioned_sync_phase(
        project,
        lambda sync_project: _sync_media_folder(
            sync_project,
            trash_retention_days,
            trash_max_size_mb,
            purge_trashed=False,
        ),
        reload_project,
    )

    # Trash purge can delete media files, so run it against a fresh project
    # snapshot after the non-destructive scan/repair phase instead of against a
    # request object that may have spent seconds ffprobing.
    project = reload_project()
    return _save_versioned_sync_phase(
        project,
        lambda sync_project: _purge_expired_trashed_assets(
            sync_project,
            retention_days=trash_retention_days,
            max_size_mb=trash_max_size_mb,
        ),
        reload_project,
    )



def _get_base_dir() -> str:
    """Get the default base directory for projects."""
    try:
        import folder_paths
        return os.path.join(folder_paths.get_output_directory(), "sonder-projects")
    except Exception:
        return ""


def _bad_project_request(message: str) -> FileNotFoundError:
    return FileNotFoundError(f"{_BAD_PROJECT_REQUEST_PREFIX}{message}")


def _configured_base_dir() -> str:
    base_dir = _get_base_dir()
    return os.path.realpath(base_dir) if base_dir else ""


def _project_dir_under_base(base_dir: str, project_id: str) -> str:
    """Return one spelling-family project path from the real scan-root anchor."""
    base_anchor = os.path.realpath(base_dir)
    lexical_path = os.path.abspath(os.path.join(base_anchor, project_id))
    return lexical_path if external_links.is_enabled() else os.path.realpath(lexical_path)


def _path_within(parent: str, child: str) -> bool:
    return _security_path_within(parent, child)


def _safe_route_token(value: str, label: str) -> str:
    try:
        return _security_safe_route_token(value, label)
    except PathSecurityError as exc:
        raise ValueError(str(exc)) from exc


def _direct_project_dir_for_cached_asset(request: web.Request) -> str | None:
    if "path" in request.query:
        return None
    project_id = _safe_route_token(request.match_info.get("project_id", ""), "project id")
    base_dir = _get_base_dir()
    if not base_dir:
        return None
    project_dir = _project_dir_under_base(base_dir, project_id)
    if not os.path.isdir(project_dir):
        return None
    base_real = os.path.realpath(base_dir)
    if not _path_within(base_real, project_dir):
        return None
    return project_dir


def _direct_project_dir_from_request(request: web.Request) -> str | None:
    if "path" in request.query:
        return None
    try:
        project_id = _safe_route_token(request.match_info.get("project_id", ""), "project id")
    except ValueError:
        return None
    base_dir = _get_base_dir()
    if not base_dir:
        return None
    base_real = os.path.realpath(base_dir)
    project_dir = _project_dir_under_base(base_real, project_id)
    if not _path_within(base_real, project_dir):
        return None
    if not os.path.isfile(os.path.join(project_dir, "project.json")):
        return None
    return project_dir


def _project_file_matches_request(data: dict, requested_id: str, folder_name: str) -> bool:
    canonical_id = str(data.get("project_id", "") or "")
    return requested_id == folder_name or requested_id == canonical_id


def _find_project_dir_in_base(base_dir: str, requested_id: str) -> str:
    base_real = os.path.realpath(base_dir)
    direct_dir = _project_dir_under_base(base_real, requested_id)
    if _path_within(base_real, direct_dir):
        direct_file = os.path.join(direct_dir, "project.json")
        if os.path.isfile(direct_file):
            return direct_dir

    try:
        entries = os.listdir(base_real)
    except OSError:
        return ""
    for entry in entries:
        try:
            safe_entry = _safe_route_token(entry, "project folder")
        except ValueError:
            continue
        entry_path = _project_dir_under_base(base_real, safe_entry)
        if not _path_within(base_real, entry_path) or not os.path.isdir(entry_path):
            continue
        pfile = os.path.join(entry_path, "project.json")
        if not os.path.isfile(pfile):
            continue
        try:
            with open(pfile, "r", encoding="utf-8") as f:
                data = json.load(f)
            if _project_file_matches_request(data, requested_id, safe_entry):
                return entry_path
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.debug("Skipping unreadable project index at %s: %s", pfile, exc)
            continue
    return ""


def _validate_loaded_project_request(project: TimelineProject, requested_id: str, project_dir: str, base_dir: str) -> None:
    base_real = os.path.realpath(base_dir)
    project_path = os.path.abspath(project_dir) if external_links.is_enabled() else os.path.realpath(project_dir)
    if not _path_within(base_real, project_path):
        raise _bad_project_request("Project path escapes configured base directory")
    folder_name = os.path.basename(os.path.normpath(project_path))
    canonical_id = str(getattr(project, "project_id", "") or "")
    if requested_id not in {folder_name, canonical_id}:
        raise _bad_project_request("Requested project id does not match loaded project")


def _reveal_in_file_manager(path: str) -> tuple[bool, str]:
    """Open the OS file manager with `path` selected/revealed. Local server only.

    Fire-and-forget via Popen (never shell=True). NOTE: Windows explorer.exe returns
    a NON-ZERO exit code even on success, so we do not wait on or gate the result by
    the process return code.
    """
    import sys
    import subprocess

    norm = os.path.normpath(path)
    try:
        if sys.platform.startswith("win"):
            # explorer.exe does its own arg parsing; as a list arg Python quotes the whole
            # token ("/select,C:\\path with spaces"), which explorer fails to parse and then
            # silently opens Documents. Pass a single command-line string so it receives
            # /select,"<path>" verbatim. This is NOT shell=True, and `norm` is containment-
            # validated (_direct_project_dir_from_request / _path_within), so no injection.
            subprocess.Popen(f'explorer /select,"{norm}"')
            return True, ""
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", norm])
            return True, ""
        parent = os.path.dirname(norm) or norm
        subprocess.Popen(["xdg-open", parent])
        return True, ""
    except FileNotFoundError:
        return False, "File manager not available on this server"
    except Exception as exc:
        logger.warning("reveal_in_file_manager failed for %s: %s", norm, exc)
        return False, "Could not open folder"


def _cached_asset_file_response(
    path: str,
    *,
    content_type: str | None = None,
) -> web.FileResponse:
    headers = dict(_ASSET_DERIVED_CACHE_HEADERS)
    if content_type:
        headers["Content-Type"] = content_type
    return web.FileResponse(path, headers=headers)


def _fast_cached_asset_response(
    request: web.Request,
    cache_subdir: str,
    filename: str,
    *,
    content_type: str | None = None,
) -> web.FileResponse | None:
    project_dir = _direct_project_dir_for_cached_asset(request)
    if not project_dir:
        return None
    _safe_route_token(request.match_info.get("asset_id", ""), "asset id")
    _safe_route_token(cache_subdir, "cache subdir")
    _safe_route_token(filename, "cached asset filename")
    target_path = resolve_project_path(
        project_dir,
        os.path.join("cache", cache_subdir, filename),
        purpose="cached asset file",
        must_exist=True,
    )
    if not target_path or not os.path.isfile(target_path):
        return None
    return _cached_asset_file_response(target_path, content_type=content_type)


def _request_if_match(request: web.Request) -> str:
    value = str(request.headers.get("If-Match", "") or "").strip()
    if not value:
        return ""
    if value.startswith("W/"):
        value = value[2:].strip()
    return value.strip('"')


def _validate_request_project_version(request: web.Request, project: TimelineProject) -> None:
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return
    expected = _request_if_match(request)
    if not expected:
        return
    actual = str(getattr(project, "modified_at", "") or "")
    if expected == actual:
        setattr(project, "_expected_modified_at", expected)
        return
    raise ProjectVersionConflict(
        project_dir=getattr(project, "project_dir", ""),
        expected_modified_at=expected,
        actual_modified_at=actual,
        current_data=project.to_dict(),
    )


def _load_project_from_request(request: web.Request, *, repair_missing_frames: bool = True) -> TimelineProject:
    """Load project from project_id path parameter."""
    if "path" in request.query:
        raise _bad_project_request("?path is no longer supported for project routes")
    try:
        project_id = _safe_route_token(request.match_info.get("project_id", ""), "project id")
    except ValueError as exc:
        raise _bad_project_request(str(exc)) from exc
    base_dir = _configured_base_dir()
    if not base_dir:
        raise _bad_project_request("Project base directory is not configured")
    project_dir = _find_project_dir_in_base(base_dir, project_id)
    if not project_dir:
        raise FileNotFoundError(f"Project not found: {project_id}")
    project = load_project(project_dir)
    _validate_loaded_project_request(project, project_id, project_dir, base_dir)
    loaded_modified_at = str(getattr(project, "modified_at", "") or "")
    if not repair_missing_frames:
        _validate_request_project_version(request, project)
        _remember_request_project(request, project)
        return project

    # Auto-repair clips/audio missing total_source_frames (field added in Phase 3)
    changed = False
    for scene in project.scenes:
        for clip in scene.clips:
            if clip.total_source_frames == 0 and clip.source_out_frame > 0:
                clip.total_source_frames = clip.source_out_frame
                changed = True
        for track in scene.audio_tracks:
            if track.total_source_frames == 0:
                duration = track.timeline_end_frame - track.timeline_start_frame + (track.source_in_frame or 0)
                if duration > 0:
                    track.total_source_frames = duration
                    changed = True
    if changed:
        # Mutation-integrity F5: this is a deterministic back-fill derived from
        # existing content, not a user edit — save WITHOUT a version bump or a
        # project_updated broadcast (a bump here would spuriously 409 mutating
        # requests' If-Match checks and trigger client refresh fanouts), and
        # best-effort: a contended save must never fail the request that merely
        # loaded the project (rare GET /scenes 500s came from atomic_replace
        # PermissionError escaping here). Repair simply re-runs on a later load.
        try:
            save_project(
                project,
                expected_modified_at=loaded_modified_at,
                bump_modified_at=False,
                notify=False,
            )
        except (ProjectVersionConflict, PermissionError) as exc:
            logger.debug("Skipped contended total_source_frames repair save for %s: %s", project_dir, exc)

    _validate_request_project_version(request, project)
    _remember_request_project(request, project)

    return project


def _session_relay_project_id(request: web.Request) -> str:
    """Validated RAW route project id for session-registry relay routes.

    The session registry is raw-string keyed and every frontend surface
    (canvas-host WS registration, tab claim/heartbeat/presence polls) keys by
    the folder basename it sends. Do NOT canonicalize to project.project_id
    here — a canonical-UUID lookup misses hosts registered under the folder id
    (the 2026-07-03 mounted-tab "Canvas not connected" regression) — and do not
    load the project from disk: the mounted tab polls widget_state every 2s.
    """
    if "path" in request.query:
        raise _bad_project_request("?path is no longer supported for project routes")
    try:
        return _safe_route_token(request.match_info.get("project_id", ""), "project id")
    except ValueError as exc:
        raise _bad_project_request(str(exc)) from exc


def _pick_active_scene(project: TimelineProject, scene_id: str = "") -> Scene | None:
    if scene_id:
        scene = project.get_scene(scene_id)
        if scene:
            return scene
    scenes = project.scenes_ordered()
    return scenes[0] if scenes else None


def _build_selection_summary(
    scene: Scene | None,
    selection_start: int = 0,
    selection_end: int = 0,
    pre_context_frames: int = 0,
    post_context_frames: int = 0,
) -> dict:
    if not scene:
        return {
            "is_full_scene": True,
            "generation_start_frame": 0,
            "generation_end_frame": 0,
            "context_start_frame": 0,
            "context_end_frame": 0,
            "frame_count": 0,
            "pre_context_frames": 0,
            "post_context_frames": 0,
            "label": "No scene selected",
        }

    duration = max(0, int(scene.duration_frames or 0))
    selection_start = max(0, _coerce_nonnegative_int(selection_start, 0))
    selection_end = max(0, _coerce_nonnegative_int(selection_end, 0))
    pre_context_frames = max(0, _coerce_nonnegative_int(pre_context_frames, 0))
    post_context_frames = max(0, _coerce_nonnegative_int(post_context_frames, 0))

    if selection_end > selection_start:
        generation_start = min(selection_start, duration)
        generation_end = min(selection_end, duration)
        is_full_scene = False
    else:
        generation_start = 0
        generation_end = duration
        is_full_scene = True

    if generation_end < generation_start:
        generation_end = generation_start

    actual_pre = min(pre_context_frames, generation_start)
    actual_post = min(post_context_frames, max(0, duration - generation_end))
    context_start = generation_start - actual_pre
    context_end = generation_end + actual_post
    frame_count = max(0, context_end - context_start)

    if is_full_scene:
        label = f"Full Scene ({duration}f)"
    else:
        label = (
            f"f{generation_start}-{generation_end} "
            f"({frame_count}f, ctx {actual_pre}/{actual_post})"
        )

    return {
        "is_full_scene": is_full_scene,
        "generation_start_frame": generation_start,
        "generation_end_frame": generation_end,
        "context_start_frame": context_start,
        "context_end_frame": context_end,
        "frame_count": frame_count,
        "pre_context_frames": actual_pre,
        "post_context_frames": actual_post,
        "label": label,
    }


def _build_dormant_summary(
    project: TimelineProject,
    scene_id: str = "",
    selection_start: int = 0,
    selection_end: int = 0,
    pre_context_frames: int = 0,
    post_context_frames: int = 0,
) -> dict:
    active_scene = _pick_active_scene(project, scene_id)

    asset_counts = {
        "video": len(project.get_assets_by_type("video")),
        "image": len(project.get_assets_by_type("image")),
        "audio": len(project.get_assets_by_type("audio")),
        "artifact": len(project.get_assets_by_type("artifact")),
    }
    asset_counts["total"] = (
        asset_counts["video"]
        + asset_counts["image"]
        + asset_counts["audio"]
        + asset_counts["artifact"]
    )

    queue_counts = {"pending": 0, "running": 0, "completed": 0, "failed": 0}
    for job in project.generation_queue:
        status = (job.status or "pending").lower()
        queue_counts[status] = queue_counts.get(status, 0) + 1
    queue_counts["total"] = len(project.generation_queue)
    active_queue_job = _pick_active_queue_job(project)

    if active_scene:
        effective_width = active_scene.width or project.resolution[0]
        effective_height = active_scene.height or project.resolution[1]
        effective_fps = active_scene.fps or project.fps
        selection = _build_selection_summary(
            active_scene,
            selection_start=selection_start,
            selection_end=selection_end,
            pre_context_frames=pre_context_frames,
            post_context_frames=post_context_frames,
        )
        # Live resolved prompt over the context-expanded window (full-scene
        # fallback lives in _build_selection_summary). Snapshot jobs carry
        # their own frozen preview_prompt — see _dormant_queue_job_payload.
        metadata = project.metadata if isinstance(getattr(project, "metadata", None), dict) else {}
        labels_on = metadata.get("prompt_channel_labels", False) is True
        delimiter = str(metadata.get("prompt_section_delimiter",
                                     prompt_payload.DEFAULT_SECTION_DELIMITER) or "")
        try:
            threshold = float(metadata.get("prompt_frame_threshold", 10.0) or 0.0)
        except (TypeError, ValueError):
            threshold = 10.0
        preview_prompt = active_scene.get_prompt_for_range(
            selection["context_start_frame"],
            selection["context_end_frame"],
            labels_on=labels_on,
            delimiter=delimiter,
            boundary_threshold_pct=threshold,
        )
        active_scene_payload = {
            "scene_id": active_scene.scene_id,
            "name": active_scene.name,
            "effective_width": effective_width,
            "effective_height": effective_height,
            "effective_fps": effective_fps,
            "duration_frames": active_scene.duration_frames,
            "clip_count": len(active_scene.clips),
            "audio_track_count": len(active_scene.audio_tracks),
            "guide_count": len(active_scene.guide_frames),
            "prompt_section_count": len(active_scene.prompt_sections),
            "selection": selection,
            "preview_prompt": preview_prompt,
        }
    else:
        active_scene_payload = None

    return {
        "project_id": project.project_id,
        "name": project.name,
        "fps": project.fps,
        "resolution": list(project.resolution),
        "modified_at": project.modified_at,
        "scene_count": len(project.scenes),
        "asset_counts": asset_counts,
        "queue_counts": queue_counts,
        "active_queue_job": _dormant_queue_job_payload(active_queue_job),
        "active_scene": active_scene_payload,
    }


def _pick_active_queue_job(project: TimelineProject) -> GenerationJob | None:
    queue = getattr(project, "generation_queue", []) or []
    for desired_status in ("running", "pending"):
        for job in queue:
            if (getattr(job, "status", "pending") or "pending").lower() == desired_status:
                return job
    return None


def _dormant_queue_job_payload(job: GenerationJob | None) -> dict | None:
    if job is None:
        return None
    return {
        "job_id": getattr(job, "job_id", ""),
        "status": getattr(job, "status", "pending") or "pending",
        "scene_id": getattr(job, "scene_id", ""),
        "scene_name": getattr(job, "scene_name", ""),
        "selection_start": int(getattr(job, "selection_start", 0) or 0),
        "selection_end": int(getattr(job, "selection_end", 0) or 0),
        "context_frames": int(getattr(job, "context_frames", 0) or 0),
        "pre_context_frames": int(getattr(job, "pre_context_frames", 0) or 0),
        "post_context_frames": int(getattr(job, "post_context_frames", 0) or 0),
        "scene_width": int(getattr(job, "scene_width", 0) or 0),
        "scene_height": int(getattr(job, "scene_height", 0) or 0),
        "scene_fps": float(getattr(job, "scene_fps", 0.0) or 0.0),
        # Frozen job.prompt verbatim — after the server-side enqueue compose
        # this IS the executed slot-9 string; recomposing here would lie for
        # pre-upgrade jobs (audit F4)
        "preview_prompt": str(getattr(job, "prompt", "") or ""),
    }


def _normalize_asset_folder(folder: str) -> str:
    return str(folder or "").strip().replace("\\", "/").strip("/")


def _query_flag(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _asset_is_trashed(asset: Asset) -> bool:
    return bool(getattr(asset, "trashed_at", ""))


def _project_trashed_assets(project: TimelineProject) -> list[Asset]:
    return [asset for asset in project.assets if _asset_is_trashed(asset)]


def _trash_project_asset(project: TimelineProject, asset: Asset) -> dict:
    _require_asset_media_source(project, asset, operation="Asset trash")
    if not _asset_is_trashed(asset):
        asset.trash_previous_folder = _normalize_asset_folder(getattr(asset, "folder", ""))
    asset.folder = ""
    asset.trashed_at = datetime.now().isoformat()
    return {
        "trashed": True,
        "asset_id": asset.asset_id,
        "trashed_at": asset.trashed_at,
        "trash_previous_folder": asset.trash_previous_folder,
    }


def _restore_project_asset(project: TimelineProject, asset: Asset) -> dict:
    restore_folder = _normalize_asset_folder(getattr(asset, "trash_previous_folder", ""))
    asset.folder = restore_folder
    asset.trashed_at = ""
    asset.trash_previous_folder = ""
    if restore_folder:
        _ensure_asset_folder(project, restore_folder)
    return {
        "restored": True,
        "asset_id": asset.asset_id,
        "folder": asset.folder,
    }


def _collect_asset_folders(project: TimelineProject) -> list[str]:
    folders = {
        _normalize_asset_folder(folder)
        for folder in project.metadata.get("asset_folders", [])
        if _normalize_asset_folder(folder)
    }
    for asset in project.assets:
        if _asset_is_trashed(asset):
            continue
        folder = _normalize_asset_folder(getattr(asset, "folder", ""))
        if folder:
            folders.add(folder)
    return sorted(folders)


def _ensure_asset_folder(project: TimelineProject, folder: str) -> None:
    normalized = _normalize_asset_folder(folder)
    if not normalized:
        return
    folders = _collect_asset_folders(project)
    if normalized not in folders:
        folders.append(normalized)
        project.metadata["asset_folders"] = sorted(folders)


def _set_asset_folders(project: TimelineProject, folders: list[str]) -> list[str]:
    normalized = sorted({
        _normalize_asset_folder(folder)
        for folder in folders
        if _normalize_asset_folder(folder)
    })
    if normalized:
        project.metadata["asset_folders"] = normalized
    else:
        project.metadata.pop("asset_folders", None)
    return normalized


def _folder_descendants(folder: str, folders: list[str]) -> list[str]:
    normalized = _normalize_asset_folder(folder)
    if not normalized:
        return []
    prefix = f"{normalized}/"
    return sorted([
        candidate for candidate in folders
        if candidate == normalized or candidate.startswith(prefix)
    ])


def _rename_folder_path(folder: str, old_folder: str, new_folder: str) -> str:
    normalized = _normalize_asset_folder(folder)
    old_normalized = _normalize_asset_folder(old_folder)
    new_normalized = _normalize_asset_folder(new_folder)
    if not normalized or not old_normalized:
        return normalized
    if normalized == old_normalized:
        return new_normalized
    prefix = f"{old_normalized}/"
    if normalized.startswith(prefix):
        suffix = normalized[len(old_normalized):].lstrip("/")
        return f"{new_normalized}/{suffix}".strip("/")
    return normalized


def _delete_folder_path(folder: str, deleted_folder: str) -> str:
    normalized = _normalize_asset_folder(folder)
    deleted_normalized = _normalize_asset_folder(deleted_folder)
    if not normalized or not deleted_normalized:
        return normalized
    if normalized == deleted_normalized:
        return ""
    prefix = f"{deleted_normalized}/"
    if normalized.startswith(prefix):
        return normalized[len(prefix):].strip("/")
    return normalized


def _rename_project_asset_folder(project: TimelineProject, folder: str, new_folder: str) -> tuple[list[str], int]:
    current = _normalize_asset_folder(folder)
    updated = _normalize_asset_folder(new_folder)
    if not current or not updated:
        raise ValueError("Both folder and new_folder are required")
    if current == updated:
        return _collect_asset_folders(project), 0
    if updated.startswith(f"{current}/"):
        raise ValueError("Cannot rename a folder into one of its own descendants")

    all_folders = _collect_asset_folders(project)
    descendants = _folder_descendants(current, all_folders)
    if not descendants:
        raise FileNotFoundError(f"Folder not found: {current}")

    renamed_descendants = {
        _rename_folder_path(candidate, current, updated)
        for candidate in descendants
    }
    existing = {
        candidate for candidate in all_folders
        if candidate not in descendants
    }
    conflicts = sorted(existing.intersection(renamed_descendants))
    if conflicts:
        raise FileExistsError(f"Folder rename would merge into existing folder: {conflicts[0]}")

    assets_moved = 0
    for asset in project.assets:
        next_folder = _rename_folder_path(asset.folder, current, updated)
        if next_folder != _normalize_asset_folder(asset.folder):
            assets_moved += 1
        asset.folder = next_folder

    next_folders = [
        _rename_folder_path(candidate, current, updated)
        for candidate in all_folders
    ]
    return _set_asset_folders(project, next_folders), assets_moved


def _folder_contains_path(folder: str, candidate: str) -> bool:
    normalized_folder = _normalize_asset_folder(folder)
    normalized_candidate = _normalize_asset_folder(candidate)
    if not normalized_folder or not normalized_candidate:
        return False
    return normalized_candidate == normalized_folder or normalized_candidate.startswith(f"{normalized_folder}/")


def _find_assets_in_folder(project: TimelineProject, folder: str) -> list[Asset]:
    current = _normalize_asset_folder(folder)
    if not current:
        raise ValueError("Folder is required")

    all_folders = _collect_asset_folders(project)
    descendants = _folder_descendants(current, all_folders)
    if not descendants:
        raise FileNotFoundError(f"Folder not found: {current}")
    return [
        asset for asset in project.assets
        if _folder_contains_path(current, getattr(asset, "folder", ""))
    ]


def _usage_sort_key(project: TimelineProject, usage: dict) -> tuple:
    scene_order = {
        scene.scene_id: index
        for index, scene in enumerate(project.scenes_ordered())
    }
    position = usage.get("start_frame")
    if position is None:
        position = usage.get("frame_index")
    if position is None:
        position = 10 ** 9
    item_id = (
        usage.get("clip_id")
        or usage.get("track_id")
        or usage.get("job_id")
        or ""
    )
    return (
        scene_order.get(usage.get("scene_id"), 10 ** 9),
        str(usage.get("scene_name") or ""),
        position,
        str(usage.get("type") or ""),
        str(item_id),
    )


def _find_asset_usages(project: TimelineProject, asset: Asset) -> dict:
    usages = []

    for scene in project.scenes_ordered():
        for clip in scene.clips:
            if clip.source_path == asset.path:
                usages.append({
                    "asset_id": asset.asset_id,
                    "type": "clip",
                    "scene_id": scene.scene_id,
                    "scene_name": scene.name,
                    "clip_id": clip.clip_id,
                    "track_index": clip.track_index,
                    "start_frame": clip.timeline_start_frame,
                    "end_frame": clip.timeline_end_frame,
                })
        for track in scene.audio_tracks:
            if track.source_path == asset.path:
                usages.append({
                    "asset_id": asset.asset_id,
                    "type": "audio_track",
                    "scene_id": scene.scene_id,
                    "scene_name": scene.name,
                    "track_id": track.track_id,
                    "lane_index": track.lane_index,
                    "start_frame": track.timeline_start_frame,
                    "end_frame": track.timeline_end_frame,
                })
        for guide in scene.guide_frames:
            if guide.asset_id == asset.asset_id:
                usages.append({
                    "asset_id": asset.asset_id,
                    "type": "guide_frame",
                    "scene_id": scene.scene_id,
                    "scene_name": scene.name,
                    "frame_index": guide.frame_index,
                    "strength": guide.strength,
                })

    for job in project.generation_queue:
        if job.result_asset_id == asset.asset_id:
            scene_name = job.scene_name
            if not scene_name and job.scene_id:
                scene = project.get_scene(job.scene_id)
                scene_name = scene.name if scene else ""
            usages.append({
                "asset_id": asset.asset_id,
                "type": "generation_job",
                "scene_id": job.scene_id,
                "scene_name": scene_name or "Project Queue",
                "job_id": job.job_id,
                "status": job.status,
            })

    usages.sort(key=lambda usage: _usage_sort_key(project, usage))
    return {
        "asset_id": asset.asset_id,
        "usages": usages,
        "usage_count": len(usages),
    }


def _aggregate_asset_usages(project: TimelineProject, assets: list[Asset]) -> dict:
    usages = []
    for asset in assets:
        usage_payload = _find_asset_usages(project, asset)
        for usage in usage_payload["usages"]:
            usages.append({
                **usage,
                "asset_name": asset.name or os.path.basename(asset.path or "") or asset.asset_id,
                "asset_path": asset.path,
                "asset_type": asset.asset_type,
            })
    usages.sort(key=lambda usage: _usage_sort_key(project, usage))
    return {
        "usages": usages,
        "usage_count": len(usages),
    }


def _favorite_asset_summary(asset: Asset) -> dict:
    return {
        "asset_id": getattr(asset, "asset_id", ""),
        "asset_name": getattr(asset, "name", "") or os.path.basename(getattr(asset, "path", "") or "") or getattr(asset, "asset_id", ""),
        "asset_path": getattr(asset, "path", ""),
        "asset_type": getattr(asset, "asset_type", ""),
    }


def _asset_trash_protection(project: TimelineProject, assets: list[Asset]) -> dict:
    usage = _aggregate_asset_usages(project, assets)
    favorites = [
        _favorite_asset_summary(asset)
        for asset in assets
        if bool(getattr(asset, "favorite", False))
    ]
    return {
        **usage,
        "favorite_count": len(favorites),
        "favorite_asset_ids": [favorite["asset_id"] for favorite in favorites],
        "favorites": favorites,
        "favorite": bool(favorites),
        "protected": usage["usage_count"] > 0 or bool(favorites),
    }


def _asset_trash_conflict_payload(project: TimelineProject, assets: list[Asset], error: str) -> dict:
    protection = _asset_trash_protection(project, assets)
    return {
        "error": error,
        "usages": protection["usages"],
        "usage_count": protection["usage_count"],
        "favorite_count": protection["favorite_count"],
        "favorite_asset_ids": protection["favorite_asset_ids"],
        "favorites": protection["favorites"],
        "favorite": protection["favorite"],
    }


def _resolve_assets_from_ids(project: TimelineProject, asset_ids) -> list[Asset]:
    if not isinstance(asset_ids, list):
        raise ValueError("asset_ids must be a list")

    resolved = []
    seen = set()
    for raw_asset_id in asset_ids:
        asset_id = str(raw_asset_id or "").strip()
        if not asset_id or asset_id in seen:
            continue
        asset = project.get_asset(asset_id)
        if not asset:
            raise FileNotFoundError(f"Asset not found: {asset_id}")
        resolved.append(asset)
        seen.add(asset_id)

    if not resolved:
        raise ValueError("asset_ids is required")
    return resolved


def _delete_asset_cache_files(project: TimelineProject, asset: Asset) -> None:
    cache_paths = _asset_cache_file_paths(project, asset)
    for cache_path in cache_paths:
        if os.path.isfile(cache_path):
            try:
                os.remove(cache_path)
            except OSError:
                logger.warning("Failed to clear asset cache file: %s", cache_path)


def _asset_storage_key(project: TimelineProject, asset: Asset, *, purpose: str) -> str:
    raw_asset_id = str(getattr(asset, "asset_id", "") or "")
    try:
        return _safe_route_token(raw_asset_id, "asset id")
    except ValueError as exc:
        log_path_quarantine(
            purpose=purpose,
            path=raw_asset_id,
            root=getattr(project, "project_dir", "") or "",
            reason=str(exc),
        )
        return ""


def _asset_cache_key(project: TimelineProject, asset: Asset) -> str:
    return _asset_storage_key(project, asset, purpose="asset cache key")


def _asset_cache_file(project: TimelineProject, asset: Asset, rel_dir: str, filename: str) -> str:
    if not filename:
        return ""
    return resolve_project_path(
        project,
        os.path.join("cache", rel_dir, filename),
        purpose="asset derived cache file",
    )


def _asset_thumbnail_path(project: TimelineProject, asset: Asset) -> str:
    cache_key = _asset_cache_key(project, asset)
    return _asset_cache_file(project, asset, "thumbnails", f"{cache_key}.png") if cache_key else ""


def _asset_cache_file_paths(project: TimelineProject, asset: Asset) -> list[str]:
    cache_key = _asset_cache_key(project, asset)
    if not cache_key:
        return []
    strip_path = _asset_cache_file(project, asset, "thumbnails", f"{cache_key}_strip.jpg")
    return [
        path for path in [
            _asset_cache_file(project, asset, "thumbnails", f"{cache_key}.png"),
            strip_path,
            _asset_cache_file(project, asset, "thumbnails", f"{cache_key}_strip.jpg.json"),
            _asset_cache_file(project, asset, "waveforms", f"{cache_key}.json"),
            _asset_cache_file(project, asset, "waveforms", f"{cache_key}_audio.wav"),
        ]
        if path
    ]


def _delete_asset_source_file(project: TimelineProject, asset: Asset, excluded_asset_ids: set[str] | None = None) -> None:
    source_path = _require_asset_media_source(project, asset, operation="Asset delete")
    if not os.path.isfile(source_path):
        return

    excluded = excluded_asset_ids or set()
    shared_source = any(
        other.asset_id != asset.asset_id
        and other.asset_id not in excluded
        and other.path == asset.path
        for other in project.assets
    )
    if shared_source:
        return

    os.remove(source_path)


def _delete_project_asset(project: TimelineProject, asset: Asset, usages_orphaned: int = 0) -> dict:
    _delete_asset_source_file(project, asset)
    _delete_asset_cache_files(project, asset)
    project.remove_asset(asset.asset_id)
    return {
        "deleted": True,
        "asset_id": asset.asset_id,
        "usages_orphaned": usages_orphaned,
    }


def _parse_trashed_at(asset: Asset) -> datetime | None:
    trashed_at = str(getattr(asset, "trashed_at", "") or "").strip()
    if not trashed_at:
        return None
    try:
        return datetime.fromisoformat(trashed_at)
    except ValueError:
        return None


def _purge_expired_trashed_assets(
    project: TimelineProject,
    retention_days: int = TRASH_RETENTION_DAYS,
    max_size_mb: float | None = None,
) -> bool:
    changed = False
    retention_days = max(0, int(retention_days if retention_days is not None else TRASH_RETENTION_DAYS))
    for asset in list(project.assets):
        parsed = _parse_trashed_at(asset)
        if not parsed:
            continue
        compare_now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        if parsed <= (compare_now - timedelta(days=retention_days)):
            try:
                _delete_project_asset(project, asset)
                changed = True
            except ValueError as exc:
                logger.warning(
                    "Skipping trashed asset purge for quarantined source %s: %s",
                    getattr(asset, "path", ""),
                    exc,
                )
            except PermissionError:
                logger.warning("Permission denied purging trashed asset: %s", getattr(asset, "path", ""))

    if max_size_mb is None:
        return changed

    max_size_bytes = int(max(0.0, float(max_size_mb)) * MB_BYTES_DECIMAL)
    trashed_assets = [
        asset for asset in project.assets
        if getattr(asset, "trashed_at", "") and _parse_trashed_at(asset)
    ]
    sizes = {
        asset.asset_id: _asset_file_size(_asset_abspath(project, asset))
        for asset in trashed_assets
    }
    total_size = sum(sizes.values())
    if total_size <= max_size_bytes:
        return changed

    def oldest_first(asset: Asset):
        parsed = _parse_trashed_at(asset)
        if parsed and parsed.tzinfo:
            parsed = parsed.astimezone().replace(tzinfo=None)
        return (parsed or datetime.min, asset.asset_id)

    for asset in sorted(trashed_assets, key=oldest_first):
        if total_size <= max_size_bytes:
            break
        try:
            _delete_project_asset(project, asset)
            total_size -= sizes.get(asset.asset_id, 0)
            changed = True
        except ValueError as exc:
            logger.warning(
                "Skipping trashed asset purge by size cap for quarantined source %s: %s",
                getattr(asset, "path", ""),
                exc,
            )
        except PermissionError:
            logger.warning("Permission denied purging trashed asset by size cap: %s", getattr(asset, "path", ""))
    return changed


def _render_cache_dir(project: TimelineProject) -> str:
    return resolve_project_path(project, os.path.join("cache", "renders"), purpose="render cache root")


def _resolve_render_cache_file(project: TimelineProject, filename: str) -> str:
    filename = str(filename or "").strip()
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or filename in {".", ".."}
        or not filename.endswith(".pt")
    ):
        raise ValueError("Invalid render cache filename")
    target_path = resolve_project_path(
        project,
        os.path.join("cache", "renders", filename),
        purpose="render cache file",
    )
    if not target_path:
        raise ValueError("Invalid render cache filename")
    return target_path


def _list_render_cache_entries(project: TimelineProject) -> list[dict]:
    cache_dir = _render_cache_dir(project)
    if not cache_dir or not os.path.isdir(cache_dir):
        return []

    entries = []
    with os.scandir(cache_dir) as scan:
        for entry in scan:
            try:
                if entry.is_symlink() or not entry.name.endswith(".pt") or not entry.is_file(follow_symlinks=False):
                    continue
                if not _path_within(cache_dir, entry.path):
                    continue
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries.append({
                "filename": entry.name,
                "mtime": stat.st_mtime,
                "size_bytes": stat.st_size,
            })
    entries.sort(key=lambda item: (item["mtime"], item["filename"]))
    return entries


def _trash_project_asset_folder(project: TimelineProject, folder: str) -> tuple[list[str], list[Asset]]:
    current = _normalize_asset_folder(folder)
    if not current:
        raise ValueError("Folder is required")

    all_folders = _collect_asset_folders(project)
    descendants = _folder_descendants(current, all_folders)
    if not descendants:
        raise FileNotFoundError(f"Folder not found: {current}")

    assets_to_trash = _find_assets_in_folder(project, current)
    _require_asset_media_sources(project, assets_to_trash, operation="Asset folder trash")
    for asset in assets_to_trash:
        _trash_project_asset(project, asset)

    remaining_folders = [
        candidate for candidate in all_folders
        if candidate not in descendants
    ]
    folders = _set_asset_folders(project, remaining_folders)
    return folders, assets_to_trash


def _delete_project_asset_folder(project: TimelineProject, folder: str) -> tuple[list[str], list[Asset]]:
    current = _normalize_asset_folder(folder)
    if not current:
        raise ValueError("Folder is required")

    all_folders = _collect_asset_folders(project)
    descendants = _folder_descendants(current, all_folders)
    if not descendants:
        raise FileNotFoundError(f"Folder not found: {current}")

    assets_to_delete = _find_assets_in_folder(project, current)
    _require_asset_media_sources(project, assets_to_delete, operation="Asset folder delete")
    deleted_ids = {asset.asset_id for asset in assets_to_delete}
    for asset in assets_to_delete:
        _delete_asset_source_file(project, asset, deleted_ids)
        _delete_asset_cache_files(project, asset)

    for asset in list(assets_to_delete):
        project.remove_asset(asset.asset_id)

    remaining_folders = [
        candidate for candidate in all_folders
        if candidate not in descendants
    ]
    folders = _set_asset_folders(project, remaining_folders)
    return folders, assets_to_delete


def _update_asset_references_for_path(project: TimelineProject, old_path: str, new_path: str) -> None:
    if not old_path or old_path == new_path:
        return

    for scene in project.scenes:
        for clip in scene.clips:
            if clip.source_path == old_path:
                clip.source_path = new_path
        for track in scene.audio_tracks:
            if track.source_path == old_path:
                track.source_path = new_path


class _StreamedAssetChanged(RuntimeError):
    pass


_ASSET_CACHE_LOCKS_GUARD = threading.Lock()
_ASSET_CACHE_LOCKS: dict[str, threading.Lock] = {}


def _asset_cache_lock(project: TimelineProject, asset: Asset) -> threading.Lock:
    key = f"{os.path.normcase(os.path.realpath(project.project_dir))}|{asset.asset_id}"
    with _ASSET_CACHE_LOCKS_GUARD:
        return _ASSET_CACHE_LOCKS.setdefault(key, threading.Lock())


def _apply_uploaded_metadata(
    asset: Asset,
    metadata: dict,
    asset_type: str,
    artifact_kind: str,
    source_path: str,
) -> None:
    asset.width = metadata["width"]
    asset.height = metadata["height"]
    asset.frame_count = metadata["frame_count"]
    asset.fps = metadata["fps"]
    asset.duration_sec = metadata["duration_sec"]
    asset.sample_rate = metadata["sample_rate"]
    asset.has_audio = metadata["has_audio"]
    asset.has_audio_checked = asset_type == "video" and _metadata_checked_for_asset(asset_type, metadata)
    asset.duration_checked = asset_type == "audio" and _metadata_checked_for_asset(asset_type, metadata)
    asset.media_probe_signature = _media_probe_signature(source_path)
    asset.artifact_kind = artifact_kind if asset_type == "artifact" else ""
    if asset_type == "video":
        apply_color_metadata(asset, metadata)
    else:
        apply_color_metadata(asset, {})


def _same_path_import_placeholder(asset: Asset) -> bool:
    return (
        not str(getattr(asset, "trashed_at", "") or "")
        and not str(getattr(asset, "prompt", "") or "")
        and not (getattr(asset, "generation_params", None) or {})
        and not str(getattr(asset, "folder", "") or "")
    )


def _unique_import_destination(project: TimelineProject, filename: str) -> tuple[str, str]:
    safe_name = sanitize_filename_component(filename, fallback="imported_media")
    known = {_normalize_project_relpath(getattr(asset, "path", "") or "") for asset in project.assets}
    for _attempt in range(128):
        rel_path = os.path.join("media", f"{uuid.uuid4().hex[:8]}_{safe_name}")
        abs_path = resolve_project_path(project, rel_path, purpose="streamed asset destination")
        if abs_path and _normalize_project_relpath(rel_path) not in known and not os.path.lexists(abs_path):
            return rel_path, abs_path
    raise RuntimeError("Could not allocate a unique project media path")


def _allocate_asset_id(project: TimelineProject) -> str:
    known = {str(getattr(asset, "asset_id", "") or "") for asset in project.assets}
    for _attempt in range(128):
        candidate = uuid.uuid4().hex[:8]
        if candidate not in known:
            return candidate
    raise RuntimeError("Could not allocate a unique asset id")


def _streamed_import_commit(project_dir: str, staged_path: str, filename: str, folder: str) -> tuple[TimelineProject, Asset]:
    asset_type, artifact_kind = _classify_asset_for_registration(staged_path)
    metadata = _extract_asset_media_metadata(
        staged_path,
        asset_type,
        strict=_media_asset_requires_probe(asset_type),
    )
    first_project = load_project(project_dir)
    rel_path, final_path = _unique_import_destination(first_project, filename)
    candidate = Asset(
        asset_id=_allocate_asset_id(first_project),
        name=filename,
        asset_type=asset_type,
        artifact_kind=artifact_kind,
        path=rel_path,
    )
    if folder:
        candidate.folder = _normalize_asset_folder(folder)
    _apply_uploaded_metadata(candidate, metadata, asset_type, artifact_kind, staged_path)
    atomic_replace(staged_path, final_path)
    candidate.media_probe_signature = _media_probe_signature(final_path)

    last_conflict = None
    try:
        for _attempt in range(3):
            current = load_project(project_dir)
            base_modified_at = str(getattr(current, "modified_at", "") or "")
            same_path = current.asset_for_source_path(rel_path)
            if same_path is not None:
                # Asset sync can observe the atomically published file before the
                # registry save. Upgrade that discovery record instead of duplicating it.
                if _same_path_import_placeholder(same_path):
                    preserved_id = same_path.asset_id
                    same_path.name = candidate.name
                    same_path.asset_type = candidate.asset_type
                    same_path.path = candidate.path
                    same_path.folder = candidate.folder
                    _apply_uploaded_metadata(same_path, metadata, asset_type, artifact_kind, final_path)
                    same_path.asset_id = preserved_id
                    committed_asset = same_path
                else:
                    committed_asset = same_path
            else:
                if current.get_asset(candidate.asset_id) is not None:
                    candidate.asset_id = _allocate_asset_id(current)
                current.add_asset(candidate)
                committed_asset = candidate
            if candidate.folder:
                _ensure_asset_folder(current, candidate.folder)
            try:
                save_project(current, expected_modified_at=base_modified_at)
                return current, committed_asset
            except ProjectVersionConflict as exc:
                last_conflict = exc
        if last_conflict is not None:
            raise last_conflict
        raise RuntimeError("Import commit retry loop exhausted")
    except Exception:
        try:
            latest = load_project(project_dir)
            if latest.asset_for_source_path(rel_path) is None and os.path.isfile(final_path):
                os.remove(final_path)
        except Exception:
            logger.warning("Could not clean an unregistered streamed import", exc_info=True)
        raise


def _is_reparse_entry(path: str) -> bool:
    parent, name = os.path.dirname(path), os.path.basename(path)
    try:
        return os.path.islink(path) or external_links.is_reparse_child(parent, name)
    except OSError:
        return os.path.islink(path)


def _create_replacement_backup(source_path: str, staging_dir: str) -> tuple[str, str]:
    extension = os.path.splitext(source_path)[1]
    backup_path = os.path.join(staging_dir, f"{uuid.uuid4().hex}.rollback{extension}")
    if _is_reparse_entry(source_path):
        os.replace(source_path, backup_path)
        return backup_path, "entry"
    try:
        os.link(source_path, backup_path, follow_symlinks=False)
        return backup_path, "hardlink"
    except (OSError, TypeError, NotImplementedError):
        ensure_upload_disk_space(staging_dir, os.path.getsize(source_path))
        shutil.copy2(source_path, backup_path)
        return backup_path, "copy"


def _rollback_same_path_replacement(final_path: str, staged_path: str, backup_path: str) -> None:
    if os.path.lexists(final_path):
        atomic_replace(final_path, staged_path)
    if os.path.lexists(backup_path):
        atomic_replace(backup_path, final_path)


def _streamed_replace_commit(
    project_dir: str,
    asset_id: str,
    initial_type: str,
    initial_path: str,
    initial_signature: str,
    staged_path: str,
    filename: str,
) -> tuple[TimelineProject, Asset]:
    replacement_type, artifact_kind = _classify_asset_for_registration(staged_path)
    if replacement_type != initial_type:
        raise ValueError(f"Replacement type mismatch: expected {initial_type}, got {replacement_type}")
    metadata = _extract_asset_media_metadata(
        staged_path,
        replacement_type,
        strict=_media_asset_requires_probe(replacement_type),
    )
    last_conflict = None

    for _attempt in range(3):
        current = load_project(project_dir)
        asset = current.get_asset(asset_id)
        if asset is None:
            raise _StreamedAssetChanged("The asset was removed while its replacement uploaded")
        current_abs = _require_asset_media_source(current, asset, operation="Asset replace")
        if (
            asset.asset_type != initial_type
            or asset.path != initial_path
            or _media_probe_signature(current_abs) != initial_signature
        ):
            raise _StreamedAssetChanged("The asset changed while its replacement uploaded; try again")

        base_modified_at = str(getattr(current, "modified_at", "") or "")
        old_rel_path = asset.path
        old_abs_path = current_abs
        old_ext = os.path.splitext(old_rel_path or "")[1].lower()
        new_ext = os.path.splitext(filename)[1].lower()
        same_path = bool(old_ext and old_ext == new_ext)
        if same_path:
            next_rel_path = old_rel_path
            next_abs_path = old_abs_path
        else:
            rel_parent = os.path.dirname(old_rel_path)
            safe_name = sanitize_filename_component(filename, fallback="replacement_media")
            for _candidate_attempt in range(128):
                next_rel_path = os.path.join(rel_parent, f"{uuid.uuid4().hex[:8]}_{safe_name}")
                next_abs_path = resolve_project_path(current, next_rel_path, purpose="streamed replacement destination")
                if next_abs_path and not os.path.lexists(next_abs_path) and current.asset_for_source_path(next_rel_path) is None:
                    break
            else:
                raise RuntimeError("Could not allocate a unique replacement path")

        backup_path = ""
        published = False
        try:
            if same_path:
                staging_dir = os.path.dirname(staged_path)
                backup_path, _backup_mode = _create_replacement_backup(next_abs_path, staging_dir)
            atomic_replace(staged_path, next_abs_path)
            published = True

            if old_rel_path != next_rel_path:
                _update_asset_references_for_path(current, old_rel_path, next_rel_path)
                asset.path = next_rel_path
            _apply_uploaded_metadata(asset, metadata, replacement_type, artifact_kind, next_abs_path)
            old_basename = os.path.basename(old_rel_path or "")
            if not asset.name or asset.name == old_basename:
                asset.name = os.path.basename(asset.path)

            try:
                save_project(current, expected_modified_at=base_modified_at)
            except ProjectVersionConflict as exc:
                last_conflict = exc
                if same_path:
                    _rollback_same_path_replacement(next_abs_path, staged_path, backup_path)
                else:
                    atomic_replace(next_abs_path, staged_path)
                published = False
                backup_path = ""
                continue

            if backup_path and os.path.lexists(backup_path):
                os.remove(backup_path)
            if old_rel_path != next_rel_path and os.path.isfile(old_abs_path):
                shared_old_path = any(
                    other.asset_id != asset.asset_id and other.path == old_rel_path
                    for other in current.assets
                )
                if not shared_old_path:
                    try:
                        os.remove(old_abs_path)
                    except OSError:
                        logger.warning("Failed to remove replaced asset file: %s", old_abs_path)
            return current, asset
        except Exception:
            if published:
                try:
                    if same_path:
                        _rollback_same_path_replacement(next_abs_path, staged_path, backup_path)
                    else:
                        atomic_replace(next_abs_path, staged_path)
                except Exception:
                    logger.exception("Failed to roll back streamed asset replacement")
            elif backup_path and os.path.lexists(backup_path) and not os.path.lexists(next_abs_path):
                try:
                    atomic_replace(backup_path, next_abs_path)
                except Exception:
                    logger.exception("Failed to restore replacement rollback entry")
            elif backup_path and os.path.lexists(backup_path):
                try:
                    os.remove(backup_path)
                except OSError:
                    logger.warning("Failed to remove unused replacement rollback entry")
            raise

    if last_conflict is not None:
        raise last_conflict
    raise RuntimeError("Replacement commit retry loop exhausted")


def _regenerate_thumbnail_if_current(project_dir: str, asset_id: str, expected_signature: str) -> None:
    project = load_project(project_dir)
    asset = project.get_asset(asset_id)
    if asset is None:
        return
    lock = _asset_cache_lock(project, asset)
    with lock:
        project = load_project(project_dir)
        asset = project.get_asset(asset_id)
        if asset is None:
            return
        source_path = _asset_abspath(project, asset)
        if not source_path or _media_probe_signature(source_path) != expected_signature:
            return
        thumb_path = _asset_thumbnail_path(project, asset)
        if not thumb_path:
            return
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        temp_path = f"{thumb_path}.{uuid.uuid4().hex}.tmp.png"
        try:
            if not ensure_thumbnail(asset.asset_type, source_path, temp_path):
                return
            latest = load_project(project_dir)
            latest_asset = latest.get_asset(asset_id)
            latest_source = _asset_abspath(latest, latest_asset) if latest_asset else ""
            if not latest_source or _media_probe_signature(latest_source) != expected_signature:
                return
            atomic_replace(temp_path, thumb_path)
        finally:
            if os.path.isfile(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass


def _delete_asset_cache_files_locked(project: TimelineProject, asset: Asset) -> None:
    with _asset_cache_lock(project, asset):
        _delete_asset_cache_files(project, asset)


def _generate_strip_if_current(project_dir: str, asset_id: str) -> bool:
    project = load_project(project_dir)
    asset = project.get_asset(asset_id)
    if asset is None or asset.asset_type != "video":
        return False
    with _asset_cache_lock(project, asset):
        project = load_project(project_dir)
        asset = project.get_asset(asset_id)
        source_path = _asset_abspath(project, asset) if asset else ""
        expected_signature = _media_probe_signature(source_path) if source_path else ""
        if not asset or asset.asset_type != "video" or not source_path or not expected_signature:
            return False
        cache_key = _asset_cache_key(project, asset)
        strip_path = _asset_cache_file(project, asset, "thumbnails", f"{cache_key}_strip.jpg")
        info_path = _asset_cache_file(project, asset, "thumbnails", f"{cache_key}_strip.jpg.json")
        if not strip_path or not info_path:
            return False
        if os.path.isfile(strip_path) and os.path.isfile(info_path):
            return True
        temp_strip = f"{strip_path}.{uuid.uuid4().hex}.tmp.jpg"
        temp_info = f"{info_path}.{uuid.uuid4().hex}.tmp.json"
        try:
            info = generate_thumbnail_strip(source_path, temp_strip)
            if not info:
                return False
            with open(temp_info, "w", encoding="utf-8") as handle:
                json.dump(info, handle)
            latest = load_project(project_dir)
            latest_asset = latest.get_asset(asset_id)
            latest_source = _asset_abspath(latest, latest_asset) if latest_asset else ""
            if not latest_source or _media_probe_signature(latest_source) != expected_signature:
                return False
            atomic_replace(temp_strip, strip_path)
            atomic_replace(temp_info, info_path)
            return True
        finally:
            for temp_path in (temp_strip, temp_info):
                if os.path.isfile(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass


def _generate_waveform_if_current(project_dir: str, asset_id: str) -> bool:
    project = load_project(project_dir)
    asset = project.get_asset(asset_id)
    if asset is None or asset.asset_type not in {"audio", "video"}:
        return False
    with _asset_cache_lock(project, asset):
        project = load_project(project_dir)
        asset = project.get_asset(asset_id)
        source_path = _asset_abspath(project, asset) if asset else ""
        expected_signature = _media_probe_signature(source_path) if source_path else ""
        if not asset or not source_path or not expected_signature:
            return False
        cache_key = _asset_cache_key(project, asset)
        waveform_path = _asset_cache_file(project, asset, "waveforms", f"{cache_key}.json")
        if not waveform_path:
            return False
        if os.path.isfile(waveform_path):
            return True
        temp_waveform = f"{waveform_path}.{uuid.uuid4().hex}.tmp.json"
        temp_audio = ""
        waveform_source = source_path
        try:
            if asset.asset_type == "video":
                temp_audio = os.path.join(
                    os.path.dirname(waveform_path),
                    f"{cache_key}.{uuid.uuid4().hex}.tmp.wav",
                )
                if not _extract_audio_from_video(source_path, temp_audio):
                    return False
                waveform_source = temp_audio
            if not generate_waveform_data(waveform_source, temp_waveform):
                return False
            latest = load_project(project_dir)
            latest_asset = latest.get_asset(asset_id)
            latest_source = _asset_abspath(latest, latest_asset) if latest_asset else ""
            if not latest_source or _media_probe_signature(latest_source) != expected_signature:
                return False
            atomic_replace(temp_waveform, waveform_path)
            return True
        finally:
            for temp_path in (temp_waveform, temp_audio):
                if temp_path and os.path.isfile(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass


def _replace_project_asset(project: TimelineProject, asset: Asset, source_path: str) -> Asset:
    old_abs_path = _require_asset_media_source(project, asset, operation="Asset replace")
    resolved_source = _resolve_source_path(source_path)
    if not resolved_source or not os.path.isfile(resolved_source):
        raise FileNotFoundError(f"File not found: {source_path}")

    replacement_type, replacement_artifact_kind = _classify_asset_for_registration(resolved_source)
    if replacement_type != asset.asset_type:
        raise ValueError(f"Replacement type mismatch: expected {asset.asset_type}, got {replacement_type}")
    metadata = _extract_asset_media_metadata(
        resolved_source,
        asset.asset_type,
        strict=_media_asset_requires_probe(asset.asset_type),
    )

    media_dir = project_media_root(project)
    if not media_dir:
        raise ValueError("Invalid project media directory")
    os.makedirs(media_dir, exist_ok=True)

    old_rel_path = asset.path
    old_ext = os.path.splitext(old_rel_path or "")[1].lower()
    new_ext = os.path.splitext(resolved_source)[1].lower()

    if old_abs_path and old_rel_path and old_ext and old_ext == new_ext:
        next_rel_path = old_rel_path
    else:
        next_rel_path = os.path.join("media", f"{uuid.uuid4().hex[:8]}_{os.path.basename(resolved_source)}")
    next_abs_path = resolve_project_path(project, next_rel_path, purpose="asset replacement destination")
    if not next_abs_path:
        raise ValueError("Invalid replacement destination")
    os.makedirs(os.path.dirname(next_abs_path), exist_ok=True)

    # In trust mode a same-extension replacement can target a linked media file.
    # Copying directly through that link would overwrite the external original; replace
    # the link itself with a normal project file instead.
    if (
        external_links.is_enabled()
        and old_rel_path == next_rel_path
        and os.path.islink(next_abs_path)
    ):
        os.unlink(next_abs_path)

    if os.path.abspath(resolved_source) != os.path.abspath(next_abs_path):
        shutil.copy2(resolved_source, next_abs_path)

    if old_rel_path != next_rel_path:
        _update_asset_references_for_path(project, old_rel_path, next_rel_path)
        asset.path = next_rel_path
        if old_abs_path and os.path.isfile(old_abs_path):
            shared_old_path = any(
                other.asset_id != asset.asset_id and other.path == old_rel_path
                for other in project.assets
            )
            if not shared_old_path:
                try:
                    os.remove(old_abs_path)
                except OSError:
                    logger.warning("Failed to remove replaced asset file: %s", old_abs_path)

    asset.width = metadata["width"]
    asset.height = metadata["height"]
    asset.frame_count = metadata["frame_count"]
    asset.fps = metadata["fps"]
    asset.duration_sec = metadata["duration_sec"]
    asset.sample_rate = metadata["sample_rate"]
    asset.has_audio = metadata["has_audio"]
    asset.has_audio_checked = asset.asset_type == "video" and _metadata_checked_for_asset(asset.asset_type, metadata)
    asset.duration_checked = asset.asset_type == "audio" and _metadata_checked_for_asset(asset.asset_type, metadata)
    asset.media_probe_signature = _media_probe_signature(next_abs_path)
    if asset.asset_type == "video":
        apply_color_metadata(asset, metadata)
    else:
        apply_color_metadata(asset, {})
    asset.artifact_kind = replacement_artifact_kind if asset.asset_type == "artifact" else ""

    old_basename = os.path.basename(old_rel_path or "")
    if not asset.name or asset.name == old_basename:
        asset.name = os.path.basename(asset.path)

    return asset


def _timeline_export_job_response(request: web.Request, job) -> dict:
    payload = job.public_status()
    if job.status != "completed":
        return payload

    result = {
        "asset": None,
        "scene": None,
        "placed_clip": job.placed_clip,
        "warnings": list(job.warnings or []),
    }
    try:
        project = _load_project_from_request(request)
        asset = project.get_asset(job.result_asset_id) if job.result_asset_id else None
        if asset:
            result["asset"] = _asset_payload(project, asset)
        scene = project.get_scene(job.result_scene_id) if job.result_scene_id else None
        if scene:
            result["scene"] = scene.to_dict()
    except Exception as exc:
        logger.warning("Failed to build timeline export completion payload: %s", exc)
        if job.result_asset_id:
            result["asset"] = {"asset_id": job.result_asset_id}
    payload["result"] = result
    return payload


def _timeline_job_matches_project(job, project: TimelineProject) -> bool:
    job_project_dir = str(getattr(job, "project_dir", "") or "")
    project_dir = str(getattr(project, "project_dir", "") or "")
    if job_project_dir and project_dir:
        return os.path.normcase(os.path.realpath(job_project_dir)) == os.path.normcase(os.path.realpath(project_dir))
    job_project_id = str(getattr(job, "project_id", "") or "")
    project_id = str(getattr(project, "project_id", "") or "")
    return bool(job_project_id and project_id and job_project_id == project_id)


if routes is not None:

    # -----------------------------------------------------------------------
    # Persistent editor tab/session ownership
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/tab/{project_id}")
    async def api_editor_tab(request: web.Request) -> web.StreamResponse:
        tab_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web", "editor.html"))
        if not os.path.isfile(tab_path):
            return _json_error("Editor tab shell not found", 404)
        return web.FileResponse(tab_path)

    @routes.get("/sonder-editor/static/{filename:.*}")
    async def api_editor_static(request: web.Request) -> web.StreamResponse:
        web_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "web"))
        requested = resolve_static_path(
            web_root,
            request.match_info.get("filename", ""),
            purpose="editor static file",
            must_exist=False,
        )
        if not requested:
            return _json_error("Invalid static path", 400)
        if not os.path.isfile(requested):
            return _json_error("Static file not found", 404)
        return web.FileResponse(requested)

    @routes.get("/sonder-editor/session/{project_id}")
    async def api_get_editor_session(request: web.Request) -> web.Response:
        remember_event_loop()
        owner = await get_owner(
            request.match_info.get("project_id", ""),
            host_id=str(request.query.get("host_id") or ""),
            source_node_id=str(request.query.get("source_node_id") or ""),
        )
        return web.json_response({"owner": owner})

    @routes.get("/sonder-editor/session/{project_id}/debug")
    async def api_debug_editor_session(request: web.Request) -> web.Response:
        remember_event_loop()
        payload = await get_project_debug_state(
            request.match_info.get("project_id", ""),
            source_node_id=str(request.query.get("source_node_id") or ""),
            host_id=str(request.query.get("host_id") or ""),
        )
        return web.json_response(payload)

    @routes.get("/sonder-editor/session/{project_id}/diag")
    async def api_diag_editor_session(request: web.Request) -> web.Response:
        remember_event_loop()
        payload = get_diag_state(request.match_info.get("project_id", ""))
        return web.json_response(payload)

    @routes.post("/sonder-editor/session/{project_id}/claim")
    async def api_claim_editor_session(request: web.Request) -> web.Response:
        remember_event_loop()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        result = await claim_session(
            request.match_info.get("project_id", ""),
            str(body.get("session_id") or ""),
            str(body.get("host_mode") or "fullscreen"),
            body.get("owner") if isinstance(body.get("owner"), dict) else body,
            str(body.get("handoff_token") or ""),
            host_id=str(body.get("host_id") or ""),
        )
        status = 200 if result.get("ok") else 409
        return web.json_response(result, status=status)

    @routes.post("/sonder-editor/session/{project_id}/heartbeat")
    async def api_heartbeat_editor_session(request: web.Request) -> web.Response:
        remember_event_loop()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        result = await heartbeat_session(
            request.match_info.get("project_id", ""),
            str(body.get("session_id") or ""),
            host_id=str(body.get("host_id") or ""),
            source_node_id=str(body.get("source_node_id") or ""),
        )
        status = 200 if result.get("ok") else 409
        return web.json_response(result, status=status)

    @routes.post("/sonder-editor/session/{project_id}/canvas_host")
    async def api_canvas_host_heartbeat(request: web.Request) -> web.Response:
        remember_event_loop()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        project_id = request.match_info.get("project_id", "")
        host_id = str(body.get("host_id") or "")
        source_node_id = str(body.get("source_node_id") or "")
        session_id = str(body.get("session_id") or "")
        result = await refresh_canvas_host(project_id, host_id, source_node_id, session_id)
        if not result.get("ok"):
            result = await register_canvas_host(
                project_id,
                source_node_id,
                session_id,
                str(body.get("workflow_id") or ""),
                str(body.get("workflow_label") or ""),
                host_id=host_id,
            )
        status = 200 if result.get("ok") else 409
        return web.json_response(result, status=status)

    @routes.post("/sonder-editor/session/{project_id}/release")
    async def api_release_editor_session(request: web.Request) -> web.Response:
        remember_event_loop()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        result = await release_session(
            request.match_info.get("project_id", ""),
            str(body.get("session_id") or ""),
            force=bool(body.get("force")),
            host_id=str(body.get("host_id") or ""),
            source_node_id=str(body.get("source_node_id") or ""),
        )
        status = 200 if result.get("ok") else 409
        return web.json_response(result, status=status)

    @routes.post("/sonder-editor/session/{project_id}/handoff")
    async def api_create_editor_handoff(request: web.Request) -> web.Response:
        remember_event_loop()
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        result = await create_handoff(
            request.match_info.get("project_id", ""),
            str(body.get("session_id") or ""),
            host_id=str(body.get("host_id") or ""),
            source_node_id=str(body.get("source_node_id") or ""),
        )
        status = 200 if result.get("ok") else 409
        return web.json_response(result, status=status)

    @routes.get("/sonder-editor/project/{project_id}/widget_state")
    async def api_get_editor_widget_state(request: web.Request) -> web.Response:
        remember_event_loop()
        try:
            project_id = _session_relay_project_id(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        payload = await get_widget_state(
            project_id,
            str(request.query.get("source_node_id") or ""),
            host_id=str(request.query.get("host_id") or ""),
        )
        return web.json_response(payload)

    @routes.put("/sonder-editor/project/{project_id}/widget_state")
    async def api_put_editor_widget_state(request: web.Request) -> web.Response:
        remember_event_loop()
        try:
            project_id = _session_relay_project_id(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            body = {}
        values = body.get("values")
        if not isinstance(values, dict):
            name = str(body.get("name") or "")
            values = {name: body.get("value")} if name else {}
        source_node_id = str(body.get("source_node_id") or request.query.get("source_node_id") or "")
        host_id = str(body.get("host_id") or request.query.get("host_id") or "")
        session_id = str(body.get("session_id") or "")
        replace = bool(body.get("replace") or body.get("seed"))
        if replace:
            payload = await seed_widget_state(
                project_id,
                source_node_id,
                session_id,
                values,
                host_id=host_id,
            )
        else:
            payload = await update_widget_state(
                project_id,
                source_node_id,
                session_id,
                values,
                host_id=host_id,
            )
        status = 200 if payload.get("ok", True) else 409
        return web.json_response(payload, status=status)

    # -----------------------------------------------------------------------
    # Install-level server settings and external project links
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/server-settings")
    async def api_get_server_settings(_request: web.Request) -> web.Response:
        return web.json_response({
            "allow_external_project_links": external_links.is_enabled(),
        })

    @routes.put("/sonder-editor/server-settings")
    async def api_update_server_settings(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        if not isinstance(body, dict) or not isinstance(body.get("allow_external_project_links"), bool):
            return _json_error("allow_external_project_links must be a boolean", 400)
        try:
            await asyncio.to_thread(external_links.set_enabled, body["allow_external_project_links"])
        except external_links.LinkError as exc:
            return _json_error(str(exc), exc.status)
        except OSError as exc:
            logger.exception("Failed to save Sonder server settings")
            return _json_error(f"Could not save server settings: {exc}", 500)
        return web.json_response({
            "allow_external_project_links": external_links.is_enabled(),
        })

    @routes.post("/sonder-editor/projects/link")
    async def api_link_project(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)
        if not isinstance(body, dict):
            return _json_error("JSON object required", 400)
        try:
            linked = await asyncio.to_thread(external_links.create_project_link, body.get("path", ""))
        except external_links.LinkError as exc:
            return _json_error(str(exc), exc.status)
        return web.json_response({"project": linked}, status=201)

    @routes.post("/sonder-editor/projects/unlink")
    async def api_unlink_project(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)
        if not isinstance(body, dict):
            return _json_error("JSON object required", 400)
        try:
            removed = await asyncio.to_thread(external_links.remove_project_link, body.get("project_id", ""))
        except external_links.LinkError as exc:
            return _json_error(str(exc), exc.status)
        if not removed:
            return _json_error("Project link not found", 404)
        return web.json_response({"ok": True})

    # -----------------------------------------------------------------------
    # Project CRUD
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/projects")
    async def api_list_projects(request: web.Request) -> web.Response:
        if "base_dir" in request.query:
            return _json_error("base_dir query is no longer supported", 400)
        base_dir = _configured_base_dir()
        if not base_dir:
            return _json_error("base_dir required", 400)
        projects = list_projects(base_dir)
        return web.json_response({"projects": projects})

    @routes.get("/sonder-editor/project/{project_id}")
    async def api_get_project(request: web.Request) -> web.Response:
        try:
            def load_and_serialize_project() -> str:
                project = _load_project_from_request(request)
                return json.dumps(project.to_dict(), ensure_ascii=False)

            body = await asyncio.to_thread(load_and_serialize_project)
            return web.Response(text=body, content_type="application/json")
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

    @routes.get("/sonder-editor/project/{project_id}/dormant_summary")
    async def api_get_dormant_summary(request: web.Request) -> web.Response:
        try:
            project = await asyncio.to_thread(_load_project_from_request, request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        summary = _build_dormant_summary(
            project,
            scene_id=request.query.get("scene_id", ""),
            selection_start=request.query.get("selection_start", 0),
            selection_end=request.query.get("selection_end", 0),
            pre_context_frames=request.query.get("pre_context_frames", 0),
            post_context_frames=request.query.get("post_context_frames", 0),
        )
        return web.json_response(summary)

    @routes.post("/sonder-editor/project")
    async def api_create_project(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        name = body.get("name", "Untitled")
        fps = body.get("fps", 24.0)
        width = body.get("width", 1280)
        height = body.get("height", 720)
        template_id = body.get("template_id", "free") or "free"
        raw_frame_constraint = body.get("frame_constraint")
        frame_constraint = raw_frame_constraint if isinstance(raw_frame_constraint, dict) and raw_frame_constraint else None
        if "base_dir" in body or "base_dir" in request.query:
            return _json_error("base_dir is no longer accepted", 400)
        base_dir = _configured_base_dir()

        if not base_dir:
            return _json_error("base_dir is required", 400)

        try:
            project = create_project(name, fps, width, height, template_id, base_dir)
            if frame_constraint is not None:
                project.frame_constraint = frame_constraint
                save_project(project)
            return web.json_response(project.to_dict(), status=201)
        except Exception as e:
            logger.exception("Failed to create project")
            return _json_error(str(e), 500)

    @routes.put("/sonder-editor/project/{project_id}")
    async def api_update_project(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        if "name" in body:
            project.name = body["name"]
        if "fps" in body:
            project.fps = float(body["fps"])
        if "resolution" in body:
            project.resolution = tuple(body["resolution"])
        if "template_id" in body:
            project.template_id = str(body.get("template_id") or "free")
        if "frame_constraint" in body:
            raw = body.get("frame_constraint")
            project.frame_constraint = raw if isinstance(raw, dict) and raw else None
        if "metadata" in body:
            project.metadata.update(body["metadata"])

        save_project(project)
        return web.json_response(project.to_dict())

    @routes.post("/sonder-editor/project/{project_id}/reveal")
    async def api_reveal_project(request: web.Request) -> web.Response:
        # Local-only convenience: open the project folder selected in the OS file
        # manager so the user can rename/delete it on disk. Path is containment-
        # validated by _direct_project_dir_from_request / project.project_dir.
        project_dir = _direct_project_dir_from_request(request)
        if not project_dir:
            try:
                project = _load_project_from_request(request)
                project_dir = getattr(project, "project_dir", "") or ""
            except FileNotFoundError as e:
                return _json_error(str(e), 404)
        if not project_dir or not os.path.isdir(project_dir):
            return _json_error("Project folder not found", 404)
        ok, err = _reveal_in_file_manager(project_dir)
        if not ok:
            return _json_error(err or "Could not open folder", 400)
        return web.json_response({"ok": True})

    # -----------------------------------------------------------------------
    # Timeline export jobs
    # -----------------------------------------------------------------------

    @routes.post("/sonder-editor/project/{project_id}/render_timeline")
    async def api_start_render_timeline(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON body", "code": "invalid_json"}, status=400)

        try:
            job = _TIMELINE_EXPORTS.start(project, body if isinstance(body, dict) else {})
        except ExportAlreadyRunning as e:
            return web.json_response(
                {"error": "export_running", "code": "export_running", "job_id": e.job_id},
                status=409,
            )
        except ValueError as e:
            return web.json_response({"error": str(e), "code": "invalid_request"}, status=400)
        except Exception as e:
            logger.exception("Failed to start timeline export")
            return web.json_response({"error": str(e), "code": "export_start_failed"}, status=500)

        return web.json_response({
            "job_id": job.job_id,
            "status": "running",
            "phase": job.phase,
        })

    @routes.get("/sonder-editor/project/{project_id}/render_timeline/{job_id}")
    async def api_get_render_timeline_job(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        job = _TIMELINE_EXPORTS.get(request.match_info.get("job_id", ""))
        if not job:
            return web.json_response({"error": "Export job not found", "code": "not_found"}, status=404)
        if not _timeline_job_matches_project(job, project):
            return web.json_response({"error": "Export job not found", "code": "not_found"}, status=404)
        return web.json_response(_timeline_export_job_response(request, job))

    @routes.post("/sonder-editor/project/{project_id}/render_timeline/{job_id}/cancel")
    async def api_cancel_render_timeline_job(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        job_id = request.match_info.get("job_id", "")
        job = _TIMELINE_EXPORTS.get(job_id)
        if not job:
            return web.json_response({"error": "Export job not found", "code": "not_found"}, status=404)
        if not _timeline_job_matches_project(job, project):
            return web.json_response({"error": "Export job not found", "code": "not_found"}, status=404)
        job = _TIMELINE_EXPORTS.cancel(job_id)
        if not job:
            return web.json_response({"error": "Export job not found", "code": "not_found"}, status=404)
        return web.json_response(job.public_status())

    # -----------------------------------------------------------------------
    # Render cache
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/project/{project_id}/cache/renders")
    async def api_list_render_cache(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        return web.json_response(_list_render_cache_entries(project))

    @routes.delete("/sonder-editor/project/{project_id}/cache/renders/{filename}")
    async def api_delete_render_cache_entry(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            cache_path = _resolve_render_cache_file(project, request.match_info.get("filename", ""))
        except ValueError as e:
            return _json_error(str(e), 400)

        if not os.path.isfile(cache_path):
            return _json_error("Render cache file not found", 404)

        try:
            os.remove(cache_path)
        except FileNotFoundError:
            return _json_error("Render cache file not found", 404)
        except PermissionError:
            logger.warning("Permission denied deleting render cache file: %s", cache_path)
            return _json_error("Render cache file is locked", 409)
        except OSError as e:
            logger.warning("Failed to delete render cache file %s: %s", cache_path, e)
            return _json_error("Failed to delete render cache file", 500)

        return web.json_response({
            "deleted": True,
            "filename": os.path.basename(cache_path),
        })

    # -----------------------------------------------------------------------
    # Asset management
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/project/{project_id}/assets")
    async def api_list_assets(request: web.Request) -> web.Response:
        """List saved assets in a project, optionally filtered by type."""
        try:
            project = await asyncio.to_thread(_load_project_from_request, request, repair_missing_frames=False)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset_type = request.query.get("type", "")
        include_trashed = _query_flag(request.query.get("include_trashed"))
        if asset_type:
            assets = project.get_assets_by_type(asset_type)
        else:
            assets = project.assets
        if not include_trashed:
            assets = [asset for asset in assets if not _asset_is_trashed(asset)]

        result = await asyncio.to_thread(_asset_payloads, project, assets)

        return web.json_response({
            "project_id": project.project_id,
            "modified_at": project.modified_at,
            "assets": result,
            "folders": _collect_asset_folders(project),
        })

    @routes.post("/sonder-editor/project/{project_id}/assets/sync")
    async def api_sync_assets(request: web.Request) -> web.Response:
        """Synchronize media-folder discovery/repair/trash cleanup, then return assets."""
        try:
            project = await asyncio.to_thread(_load_project_from_request, request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        trash_retention_days = _query_nonnegative_int(
            request.query.get("retention_days"),
            TRASH_RETENTION_DAYS,
        )
        trash_max_size_mb = _query_optional_nonnegative_float(
            request.query.get("max_size_mb"),
            None,
        )

        project = await asyncio.to_thread(
            _sync_media_folder_versioned,
            project,
            trash_retention_days,
            trash_max_size_mb,
            reload_project=lambda: _load_project_from_request(request),
        )
        _remember_request_project(request, project)

        asset_type = request.query.get("type", "")
        include_trashed = _query_flag(request.query.get("include_trashed"))
        if asset_type:
            assets = project.get_assets_by_type(asset_type)
        else:
            assets = project.assets
        if not include_trashed:
            assets = [asset for asset in assets if not _asset_is_trashed(asset)]

        result = await asyncio.to_thread(_asset_payloads, project, assets)
        return web.json_response({
            "project_id": project.project_id,
            "modified_at": project.modified_at,
            "assets": result,
            "folders": _collect_asset_folders(project),
        })

    @routes.get("/sonder-editor/project/{project_id}/assets/dormant")
    async def api_list_dormant_assets(request: web.Request) -> web.Response:
        """List lightweight asset data without scanning/syncing media folders."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        include_trashed = _query_flag(request.query.get("include_trashed"))
        assets = project.assets if include_trashed else [asset for asset in project.assets if not _asset_is_trashed(asset)]
        result = await asyncio.to_thread(_asset_payloads, project, assets)

        return web.json_response({
            "project_id": project.project_id,
            "modified_at": project.modified_at,
            "assets": result,
            "folders": _collect_asset_folders(project),
        })

    @routes.post("/sonder-editor/project/{project_id}/assets/import")
    async def api_import_asset(request: web.Request) -> web.Response:
        """Import a media file into the project's media directory."""
        request_content_type = getattr(request, "content_type", "application/json")
        if request_content_type == "multipart/form-data":
            try:
                project = await asyncio.to_thread(_load_project_from_request, request, repair_missing_frames=False)
            except FileNotFoundError as e:
                return _json_error(str(e), 404)
            media_dir = project_media_root(project)
            if not media_dir:
                return _json_error("Invalid project media directory", 400)
            started_at = time.monotonic()
            try:
                async with receive_project_upload(
                    request,
                    media_dir,
                    allowed_text_fields={"folder"},
                ) as upload:
                    folder = _normalize_asset_folder(upload.fields.get("folder", ""))
                    committed_project, asset = await asyncio.to_thread(
                        _streamed_import_commit,
                        project.project_dir,
                        upload.path,
                        upload.filename,
                        folder,
                    )
                    if asset.asset_type in {"video", "image", "audio"}:
                        await asyncio.to_thread(
                            _regenerate_thumbnail_if_current,
                            committed_project.project_dir,
                            asset.asset_id,
                            asset.media_probe_signature,
                        )
                    payload = await asyncio.to_thread(_asset_payload, committed_project, asset)
                    logger.info(
                        "Streamed asset import completed bytes=%s duration_ms=%s active=%s",
                        upload.size,
                        round((time.monotonic() - started_at) * 1000),
                        active_upload_count(),
                    )
                    return web.json_response(payload, status=201)
            except UploadRequestError as exc:
                logger.info(
                    "Streamed asset import rejected status=%s duration_ms=%s active=%s",
                    exc.status,
                    round((time.monotonic() - started_at) * 1000),
                    active_upload_count(),
                )
                return _json_error(exc.message, exc.status)
            except (ValueError, MediaProbeError) as exc:
                logger.info(
                    "Streamed asset import validation failed error_type=%s duration_ms=%s active=%s",
                    type(exc).__name__,
                    round((time.monotonic() - started_at) * 1000),
                    active_upload_count(),
                )
                return _json_error(str(exc), 400)

        if request_content_type != "application/json":
            return _json_error("Content-Type must be application/json or multipart/form-data", 415)

        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        source_path = _resolve_source_path(body.get("source_path", ""))

        if not source_path or not os.path.isfile(source_path):
            return _json_error(f"File not found: {source_path}", 400)

        requested_type = str(body.get("type") or "").strip().lower()
        asset_type, artifact_kind = _classify_asset_for_registration(source_path)
        if requested_type in {"video", "image", "audio", "artifact"}:
            asset_type = requested_type
            artifact_kind = artifact_kind if asset_type == "artifact" else ""

        # Copy to project media directory
        media_dir = project_media_root(project)
        if not media_dir:
            return _json_error("Invalid project media directory", 400)
        os.makedirs(media_dir, exist_ok=True)
        basename = os.path.basename(source_path)
        dest_filename = f"{uuid.uuid4().hex[:8]}_{basename}"
        dest_rel_path = os.path.join("media", dest_filename)
        dest_path = resolve_project_path(project, dest_rel_path, purpose="asset import destination")
        if not dest_path:
            return _json_error("Invalid asset import destination", 400)
        shutil.copy2(source_path, dest_path)

        try:
            metadata = _extract_asset_media_metadata(
                dest_path,
                asset_type,
                strict=_media_asset_requires_probe(asset_type),
            )
        except MediaProbeError as exc:
            try:
                os.remove(dest_path)
            except OSError:
                pass
            return _json_error(str(exc), 400)

        # Create asset entry
        asset = Asset(
            name=basename,
            asset_type=asset_type,
            artifact_kind=artifact_kind,
            path=dest_rel_path,
            width=metadata["width"],
            height=metadata["height"],
            frame_count=metadata["frame_count"],
            fps=metadata["fps"],
            duration_sec=metadata["duration_sec"],
            sample_rate=metadata["sample_rate"],
            has_audio=metadata["has_audio"],
            has_audio_checked=asset_type == "video" and _metadata_checked_for_asset(asset_type, metadata),
            duration_checked=asset_type == "audio" and _metadata_checked_for_asset(asset_type, metadata),
            media_probe_signature=_media_probe_signature(dest_path),
            prompt=body.get("prompt", ""),
            generation_params=body.get("generation_params", {}),
        )
        if asset_type == "video":
            apply_color_metadata(asset, metadata)
        if body.get("folder"):
            asset.folder = _normalize_asset_folder(body["folder"])
            _ensure_asset_folder(project, asset.folder)
        project.add_asset(asset)
        save_project(project)

        if asset_type in {"video", "image", "audio"}:
            thumb_path = _asset_thumbnail_path(project, asset)
            if thumb_path:
                ensure_thumbnail(asset_type, dest_path, thumb_path)

        return web.json_response(_asset_payload(project, asset), status=201)

    @routes.post("/sonder-editor/project/{project_id}/assets/folders")
    async def api_create_asset_folder(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        folder = _normalize_asset_folder(body.get("folder", ""))
        if not folder:
            return _json_error("Folder name required", 400)

        _ensure_asset_folder(project, folder)
        save_project(project)
        return web.json_response({"folders": _collect_asset_folders(project)})

    @routes.put("/sonder-editor/project/{project_id}/assets/folders")
    async def api_rename_asset_folder(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        try:
            folders, assets_moved = _rename_project_asset_folder(
                project,
                body.get("old_folder", ""),
                body.get("new_folder", ""),
            )
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except FileExistsError as e:
            return _json_error(str(e), 409)
        except ValueError as e:
            return _json_error(str(e), 400)

        save_project(project)
        return web.json_response({"folders": folders, "assets_moved": assets_moved})

    @routes.delete("/sonder-editor/project/{project_id}/assets/folders")
    async def api_delete_asset_folder(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        body = {}
        try:
            body = await request.json()
        except Exception:
            pass

        folder = body.get("folder", "")
        try:
            assets_to_delete = _find_assets_in_folder(project, folder)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ValueError as e:
            return _json_error(str(e), 400)

        force = bool(body.get("force", False))
        if not force and assets_to_delete:
            protection = _asset_trash_protection(project, assets_to_delete)
            if protection["protected"]:
                return web.json_response(
                    _asset_trash_conflict_payload(project, assets_to_delete, "One or more assets in this folder are used or favorited"),
                    status=409,
                )

        try:
            _folders, trashed_assets = _trash_project_asset_folder(project, folder)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ValueError as e:
            return _json_error(str(e), 400)
        except OSError as e:
            return _json_error(str(e), 500)

        save_project(project)
        return web.json_response({
            "trashed_folder": _normalize_asset_folder(folder),
            "trashed_assets": len(trashed_assets),
        })

    @routes.post("/sonder-editor/project/{project_id}/assets/viewport_snapshot")
    async def api_viewport_snapshot_asset(request: web.Request) -> web.Response:
        """Register a browser-captured viewport source-frame snapshot as an image asset."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            reader = await request.multipart()
            file_bytes = None
            metadata = {}
            async for field in reader:
                if field.name == "file":
                    file_bytes = await field.read(decode=False)
                elif field.name == "metadata":
                    raw = await field.text()
                    metadata = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return _json_error("Invalid metadata JSON", 400)
        except Exception as e:
            return _json_error(f"Invalid multipart body: {e}", 400)

        if not file_bytes:
            return _json_error("file is required", 400)
        if len(file_bytes) > 50 * 1024 * 1024:
            return _json_error("Snapshot file is too large", 413)

        source_path = str(metadata.get("source_path") or "")
        if not source_path:
            return _json_error("metadata.source_path is required", 400)
        if _project_asset_for_source_path(project, source_path) is None:
            return _json_error("source_path is not a project asset", 400)

        source_frame_index = _coerce_nonnegative_int(metadata.get("source_frame_index"), 0)
        timeline_frame_index = _coerce_nonnegative_int(metadata.get("timeline_frame_index"), 0)
        snapshot_long_edge = _coerce_nonnegative_int(metadata.get("snapshot_long_edge"), 0)
        snapshot_source_long_edge = _coerce_nonnegative_int(metadata.get("snapshot_source_long_edge"), 0)

        try:
            from io import BytesIO
            from PIL import Image

            with Image.open(BytesIO(file_bytes)) as img:
                w, h = img.size
                if w <= 0 or h <= 0:
                    return _json_error("Snapshot image has invalid dimensions", 400)
        except Exception as e:
            return _json_error(f"Invalid snapshot image: {e}", 400)

        try:
            media_dir = project_media_root(project)
            if not media_dir:
                return _json_error("Invalid project media directory", 400)
            os.makedirs(media_dir, exist_ok=True)
            out_filename = f"{uuid.uuid4().hex[:8]}_snapshot_f{source_frame_index}.png"
            out_rel_path = os.path.join("media", out_filename)
            out_path = resolve_project_path(project, out_rel_path, purpose="viewport snapshot destination")
            if not out_path:
                return _json_error("Invalid snapshot destination", 400)
            with open(out_path, "wb") as handle:
                handle.write(file_bytes)

            generation_params = {
                **(metadata if isinstance(metadata, dict) else {}),
                "source_path": source_path,
                "source_frame_index": source_frame_index,
                "timeline_frame_index": timeline_frame_index,
                "extraction_mode": "viewport_snapshot",
                "snapshot_long_edge": snapshot_long_edge,
                "snapshot_source_long_edge": snapshot_source_long_edge,
            }
            asset = Asset(
                name=f"Viewport Snapshot {timeline_frame_index}",
                asset_type="image",
                path=out_rel_path,
                width=w,
                height=h,
                generation_params=generation_params,
            )
            project.add_asset(asset)
            save_project(project)

            thumb_path = _asset_thumbnail_path(project, asset)
            if thumb_path:
                ensure_thumbnail("image", out_path, thumb_path)
            return web.json_response(_asset_payload(project, asset), status=201)
        except Exception as e:
            logger.warning("Failed to register viewport snapshot: %s", e)
            return _json_error(str(e), 500)

    @routes.post("/sonder-editor/project/{project_id}/assets/extract_frame")
    async def api_extract_frame(request: web.Request) -> web.Response:
        """Extract a single video frame and save as an image asset."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)
        frame_index = _coerce_nonnegative_int(body.get("frame_index"), 0)
        target_long_edge = _coerce_nonnegative_int(body.get("target_long_edge"), 0)

        try:
            source_path, abs_path = _extract_frame_source(project, body)
        except ValueError as e:
            return _json_error(str(e), 400)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            media_dir = project_media_root(project)
            if not media_dir:
                return _json_error("Invalid project media directory", 400)
            os.makedirs(media_dir, exist_ok=True)
            out_filename = f"{uuid.uuid4().hex[:8]}_frame_{frame_index}.png"
            out_rel_path = os.path.join("media", out_filename)
            out_path = resolve_project_path(project, out_rel_path, purpose="frame extraction destination")
            if not out_path:
                return _json_error("Invalid frame extraction destination", 400)

            # Prefer ffmpeg for frame extraction to preserve video decode fidelity.
            # Fall back to OpenCV if ffmpeg is unavailable or fails.
            source_asset = _project_asset_for_source_path(project, source_path)
            interpretation = resolve_source_color_interpretation(source_asset, abs_path)
            frame_rgb = decode_video_frame(abs_path, frame_index, color_interpretation=interpretation)
            extraction_mode = "ffmpeg"
            if frame_rgb is None:
                import cv2
                cap = cv2.VideoCapture(abs_path, cv2.CAP_FFMPEG)
                try:
                    if not cap.isOpened():
                        return _json_error("Could not open video file", 500)

                    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                    ret, frame_bgr = cap.read()
                finally:
                    cap.release()

                if not ret:
                    return _json_error(f"Could not read frame {frame_index}", 500)
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                frame_rgb = apply_rgb_color_correction(
                    frame_rgb, color_correction_for_interpretation(interpretation)
                )
                extraction_mode = "opencv_fallback"

            if target_long_edge > 0:
                frame_rgb = resize_frame_to_long_edge(frame_rgb, target_long_edge)
            h, w = frame_rgb.shape[:2]
            write_png(out_path, frame_rgb)

            asset = Asset(
                name=f"Frame {frame_index}",
                asset_type="image",
                path=out_rel_path,
                width=w,
                height=h,
                generation_params={
                    "source_path": source_path,
                    "source_frame_index": frame_index,
                    "extraction_mode": extraction_mode,
                    "target_long_edge": target_long_edge,
                },
            )
            project.add_asset(asset)
            save_project(project)

            # Generate thumbnail
            thumb_path = _asset_thumbnail_path(project, asset)
            if thumb_path:
                ensure_thumbnail("image", out_path, thumb_path)

            return web.json_response(_asset_payload(project, asset), status=201)

        except ImportError:
            return _json_error("cv2 (OpenCV) not available", 500)
        except Exception as e:
            logger.warning("Failed to extract frame: %s", e)
            return _json_error(str(e), 500)

    @routes.put("/sonder-editor/project/{project_id}/assets/bulk-move")
    async def api_bulk_move_assets(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        try:
            assets = _resolve_assets_from_ids(project, body.get("asset_ids", []))
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ValueError as e:
            return _json_error(str(e), 400)

        folder = _normalize_asset_folder(body.get("folder", ""))
        if folder:
            _ensure_asset_folder(project, folder)

        for asset in assets:
            asset.folder = folder

        save_project(project)
        return web.json_response({"updated": len(assets)})

    @routes.post("/sonder-editor/project/{project_id}/assets/bulk-usages")
    async def api_bulk_asset_usages(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        try:
            assets = _resolve_assets_from_ids(project, body.get("asset_ids", []))
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ValueError as e:
            return _json_error(str(e), 400)

        return web.json_response(_aggregate_asset_usages(project, assets))

    @routes.post("/sonder-editor/project/{project_id}/assets/bulk-delete")
    async def api_bulk_delete_assets(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        try:
            assets = _resolve_assets_from_ids(project, body.get("asset_ids", []))
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ValueError as e:
            return _json_error(str(e), 400)

        force = bool(body.get("force", False))
        if not force:
            protection = _asset_trash_protection(project, assets)
            if protection["protected"]:
                return web.json_response(
                    _asset_trash_conflict_payload(project, assets, "One or more assets are used or favorited"),
                    status=409,
                )

        trashed_ids = []
        try:
            _require_asset_media_sources(project, assets, operation="Asset bulk trash")
            for asset in assets:
                _trash_project_asset(project, asset)
                trashed_ids.append(asset.asset_id)
        except ValueError as e:
            return _json_error(str(e), 400)
        save_project(project)
        return web.json_response({
            "trashed": trashed_ids,
        })

    @routes.post("/sonder-editor/project/{project_id}/assets/restore")
    async def api_restore_asset(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        asset_id = str(body.get("asset_id", "")).strip()
        if not asset_id:
            return _json_error("asset_id is required", 400)

        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        payload = _restore_project_asset(project, asset)
        save_project(project)
        return web.json_response({
            **payload,
            "asset": _asset_payload(project, asset),
        })

    @routes.post("/sonder-editor/project/{project_id}/assets/bulk-restore")
    async def api_bulk_restore_assets(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        try:
            assets = _resolve_assets_from_ids(project, body.get("asset_ids", []))
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ValueError as e:
            return _json_error(str(e), 400)

        restored_ids = []
        for asset in assets:
            _restore_project_asset(project, asset)
            restored_ids.append(asset.asset_id)

        save_project(project)
        return web.json_response({"restored": restored_ids})

    @routes.post("/sonder-editor/project/{project_id}/assets/permanent")
    async def api_permanent_delete_asset(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        asset_id = str(body.get("asset_id", "")).strip()
        if not asset_id:
            return _json_error("asset_id is required", 400)

        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        usage = _find_asset_usages(project, asset)
        force = bool(body.get("force", False))
        if usage["usage_count"] > 0 and not force:
            return web.json_response({
                "error": "Asset is in use",
                "usages": usage["usages"],
                "usage_count": usage["usage_count"],
            }, status=409)

        try:
            payload = _delete_project_asset(project, asset, usage["usage_count"])
        except ValueError as e:
            return _json_error(str(e), 400)
        except OSError as e:
            return _json_error(str(e), 500)

        save_project(project)
        return web.json_response(payload)

    @routes.post("/sonder-editor/project/{project_id}/assets/bulk-permanent-delete")
    async def api_bulk_permanent_delete_assets(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        try:
            assets = _resolve_assets_from_ids(project, body.get("asset_ids", []))
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ValueError as e:
            return _json_error(str(e), 400)

        usage = _aggregate_asset_usages(project, assets)
        force = bool(body.get("force", False))
        if usage["usage_count"] > 0 and not force:
            return web.json_response({
                "error": "One or more assets are still in use",
                "usages": usage["usages"],
                "usage_count": usage["usage_count"],
            }, status=409)

        deleted_ids = []
        try:
            _require_asset_media_sources(project, assets, operation="Asset bulk delete")
            for asset in assets:
                _delete_project_asset(project, asset)
                deleted_ids.append(asset.asset_id)
        except ValueError as e:
            return _json_error(str(e), 400)
        except OSError as e:
            return _json_error(str(e), 500)

        save_project(project)
        return web.json_response({
            "deleted": deleted_ids,
            "usages_orphaned": usage["usage_count"],
        })

    @routes.post("/sonder-editor/project/{project_id}/assets/empty-trash")
    async def api_empty_asset_trash(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        deleted_ids = []
        try:
            trashed_assets = list(_project_trashed_assets(project))
            _require_asset_media_sources(project, trashed_assets, operation="Asset empty trash")
            for asset in trashed_assets:
                _delete_project_asset(project, asset)
                deleted_ids.append(asset.asset_id)
        except ValueError as e:
            return _json_error(str(e), 400)
        except OSError as e:
            return _json_error(str(e), 500)

        save_project(project)
        return web.json_response({
            "deleted": deleted_ids,
            "emptied": len(deleted_ids),
        })

    @routes.put("/sonder-editor/project/{project_id}/assets/{asset_id}")
    async def api_update_asset(request: web.Request) -> web.Response:
        """Update asset properties (e.g. name)."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset_id = request.match_info["asset_id"]
        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        if "name" in body:
            asset.name = str(body["name"]).strip() or asset.name
        if "folder" in body:
            asset.folder = _normalize_asset_folder(body["folder"])
            _ensure_asset_folder(project, asset.folder)
        if "favorite" in body:
            asset.favorite = bool(body["favorite"])

        save_project(project)
        return web.json_response(_asset_payload(project, asset))

    @routes.get("/sonder-editor/project/{project_id}/assets/{asset_id}/workflow")
    async def api_get_asset_workflow(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset_id = request.match_info["asset_id"]
        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        workflow = _extract_embedded_workflow(project, asset)
        if workflow is None:
            return web.json_response({"reason": "unavailable"}, status=404)
        return web.json_response({"workflow": workflow, "source": "embedded"})

    @routes.get("/sonder-editor/project/{project_id}/assets/{asset_id}/usages")
    async def api_get_asset_usages(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset_id = request.match_info["asset_id"]
        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        return web.json_response(_find_asset_usages(project, asset))

    @routes.delete("/sonder-editor/project/{project_id}/assets/{asset_id}")
    async def api_delete_asset(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset_id = request.match_info["asset_id"]
        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        body = {}
        try:
            parsed_body = await request.json()
            if isinstance(parsed_body, dict):
                body = parsed_body
        except Exception:
            pass
        force = bool(body.get("force", False))
        if not force:
            protection = _asset_trash_protection(project, [asset])
            if protection["protected"]:
                return web.json_response(
                    _asset_trash_conflict_payload(project, [asset], "Asset is used or favorited"),
                    status=409,
                )

        try:
            payload = _trash_project_asset(project, asset)
        except ValueError as e:
            return _json_error(str(e), 400)
        save_project(project)
        return web.json_response(payload)

    @routes.post("/sonder-editor/project/{project_id}/assets/{asset_id}/replace")
    async def api_replace_asset(request: web.Request) -> web.Response:
        request_content_type = getattr(request, "content_type", "application/json")
        if request_content_type == "multipart/form-data":
            try:
                initial_project = await asyncio.to_thread(_load_project_from_request, request, repair_missing_frames=False)
            except FileNotFoundError as e:
                return _json_error(str(e), 404)
            asset_id = request.match_info["asset_id"]
            initial_asset = initial_project.get_asset(asset_id)
            if not initial_asset:
                return _json_error(f"Asset not found: {asset_id}", 404)
            try:
                initial_abs_path = await asyncio.to_thread(
                    _require_asset_media_source,
                    initial_project,
                    initial_asset,
                    operation="Asset replace",
                )
                initial_signature = await asyncio.to_thread(_media_probe_signature, initial_abs_path)
            except (FileNotFoundError, ValueError) as exc:
                return _json_error(str(exc), 400)
            started_at = time.monotonic()
            try:
                async with receive_project_upload(
                    request,
                    os.path.dirname(initial_abs_path),
                    allowed_text_fields=set(),
                ) as upload:
                    committed_project, committed_asset = await asyncio.to_thread(
                        _streamed_replace_commit,
                        initial_project.project_dir,
                        asset_id,
                        initial_asset.asset_type,
                        initial_asset.path,
                        initial_signature,
                        upload.path,
                        upload.filename,
                    )
                    await asyncio.to_thread(
                        _delete_asset_cache_files_locked,
                        committed_project,
                        committed_asset,
                    )
                    await asyncio.to_thread(
                        _regenerate_thumbnail_if_current,
                        committed_project.project_dir,
                        committed_asset.asset_id,
                        committed_asset.media_probe_signature,
                    )
                    payload = await asyncio.to_thread(
                        lambda: {
                            "asset": _asset_payload(committed_project, committed_asset),
                            "usage": _find_asset_usages(committed_project, committed_asset),
                        }
                    )
                    logger.info(
                        "Streamed asset replacement completed bytes=%s duration_ms=%s active=%s",
                        upload.size,
                        round((time.monotonic() - started_at) * 1000),
                        active_upload_count(),
                    )
                    return web.json_response(payload)
            except UploadRequestError as exc:
                logger.info(
                    "Streamed asset replacement rejected status=%s duration_ms=%s active=%s",
                    exc.status,
                    round((time.monotonic() - started_at) * 1000),
                    active_upload_count(),
                )
                return _json_error(exc.message, exc.status)
            except _StreamedAssetChanged as exc:
                logger.info(
                    "Streamed asset replacement conflicted duration_ms=%s active=%s",
                    round((time.monotonic() - started_at) * 1000),
                    active_upload_count(),
                )
                return _json_error(str(exc), 409)
            except (ValueError, MediaProbeError) as exc:
                logger.info(
                    "Streamed asset replacement validation failed error_type=%s duration_ms=%s active=%s",
                    type(exc).__name__,
                    round((time.monotonic() - started_at) * 1000),
                    active_upload_count(),
                )
                return _json_error(str(exc), 400)

        if request_content_type != "application/json":
            return _json_error("Content-Type must be application/json or multipart/form-data", 415)

        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset_id = request.match_info["asset_id"]
        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        source_path = body.get("source_path", "")
        try:
            _replace_project_asset(project, asset, source_path)
        except FileNotFoundError as e:
            return _json_error(str(e), 400)
        except (ValueError, MediaProbeError) as e:
            return _json_error(str(e), 400)

        _delete_asset_cache_files(project, asset)

        if not _asset_missing(project, asset):
            thumb_path = _asset_thumbnail_path(project, asset)
            if thumb_path:
                ensure_thumbnail(asset.asset_type, _asset_abspath(project, asset), thumb_path)

        save_project(project)
        return web.json_response({
            "asset": _asset_payload(project, asset),
            "usage": _find_asset_usages(project, asset),
        })

    @routes.get("/sonder-editor/project/{project_id}/thumbnail/{asset_id}")
    async def api_get_thumbnail(request: web.Request) -> web.Response:
        """Serve a thumbnail image for an asset."""
        try:
            asset_id = _safe_route_token(request.match_info["asset_id"], "asset id")
            fast_response = _fast_cached_asset_response(request, "thumbnails", f"{asset_id}.png")
        except ValueError as e:
            return _json_error(str(e), 400)
        if fast_response is not None:
            return fast_response

        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        thumb_path = _asset_thumbnail_path(project, asset)
        if not thumb_path:
            return _json_error("Thumbnail unavailable for missing asset", 404)
        if os.path.isfile(thumb_path):
            return _cached_asset_file_response(thumb_path)

        source_path = _asset_abspath(project, asset)
        if not source_path or not os.path.isfile(source_path):
            return _json_error("Thumbnail unavailable for missing asset", 404)
        # Cache-miss generation calls ffmpeg synchronously; run off the event
        # loop so heartbeats and the sweeper keep firing.
        expected_signature = await asyncio.to_thread(_media_probe_signature, source_path)
        await asyncio.to_thread(
            _regenerate_thumbnail_if_current,
            project.project_dir,
            asset.asset_id,
            expected_signature,
        )
        if not os.path.isfile(thumb_path):
            return _json_error("Failed to generate thumbnail", 500)

        return _cached_asset_file_response(thumb_path)

    @routes.get("/sonder-editor/project/{project_id}/thumbnail_strip/{asset_id}")
    async def api_get_thumbnail_strip(request: web.Request) -> web.Response:
        """Serve a filmstrip thumbnail for a video asset (tiled frames)."""
        try:
            asset_id = _safe_route_token(request.match_info["asset_id"], "asset id")
            if request.query.get("info"):
                fast_response = _fast_cached_asset_response(
                    request,
                    "thumbnails",
                    f"{asset_id}_strip.jpg.json",
                    content_type="application/json",
                )
            else:
                fast_response = _fast_cached_asset_response(request, "thumbnails", f"{asset_id}_strip.jpg")
        except ValueError as e:
            return _json_error(str(e), 400)
        if fast_response is not None:
            return fast_response

        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset = project.get_asset(asset_id)
        if not asset or asset.asset_type != "video":
            return _json_error(f"Video asset not found: {asset_id}", 404)

        cache_key = _asset_cache_key(project, asset)
        if not cache_key:
            return _json_error("Thumbnail strip unavailable for missing asset", 404)
        strip_path = _asset_cache_file(project, asset, "thumbnails", f"{cache_key}_strip.jpg")
        info_path = _asset_cache_file(project, asset, "thumbnails", f"{cache_key}_strip.jpg.json")
        if not strip_path or not info_path:
            return _json_error("Thumbnail strip unavailable for missing asset", 404)

        # Generate if not cached. ffmpeg call must stay off the event loop.
        if not os.path.isfile(strip_path):
            ok = await asyncio.to_thread(_generate_strip_if_current, project.project_dir, asset.asset_id)
            if not ok:
                return _json_error("Failed to generate thumbnail strip", 500)

        # Return info JSON or image
        if request.query.get("info"):
            if os.path.isfile(info_path):
                return _cached_asset_file_response(info_path, content_type="application/json")
            return _json_error("Strip info not found", 404)

        return _cached_asset_file_response(strip_path)

    @routes.get("/sonder-editor/project/{project_id}/waveform/{asset_id}")
    async def api_get_waveform(request: web.Request) -> web.Response:
        """Serve waveform peaks data for an audio asset or a video asset with audio."""
        try:
            asset_id = _safe_route_token(request.match_info["asset_id"], "asset id")
            fast_response = _fast_cached_asset_response(
                request,
                "waveforms",
                f"{asset_id}.json",
                content_type="application/json",
            )
        except ValueError as e:
            return _json_error(str(e), 400)
        if fast_response is not None:
            return fast_response

        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset = project.get_asset(asset_id)
        if not asset or asset.asset_type not in {"audio", "video"}:
            return _json_error(f"Audio-capable asset not found: {asset_id}", 404)
        if asset.asset_type == "video" and not asset.has_audio:
            return _json_error("Waveform unavailable for video without audio", 404)

        cache_key = _asset_cache_key(project, asset)
        if not cache_key:
            return _json_error("Waveform unavailable for missing asset", 404)
        waveform_path = _asset_cache_file(project, asset, "waveforms", f"{cache_key}.json")
        if not waveform_path:
            return _json_error("Waveform unavailable for missing asset", 404)

        # Generate if not cached. ffmpeg calls must stay off the event loop.
        if not os.path.isfile(waveform_path):
            ok = await asyncio.to_thread(_generate_waveform_if_current, project.project_dir, asset.asset_id)
            if not ok:
                return _json_error("Failed to generate waveform data", 500)

        return _cached_asset_file_response(waveform_path, content_type="application/json")

    # -----------------------------------------------------------------------
    # Scene CRUD
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/project/{project_id}/scenes")
    async def api_list_scenes(request: web.Request) -> web.Response:
        try:
            # Off-loop load (mutation-integrity F5, parity with api_list_assets):
            # the loader can run the repair save whose atomic_replace retry
            # blocks up to ~375 ms — never on the aiohttp event loop.
            def load_and_serialize_scenes() -> str:
                project = _load_project_from_request(request)
                return json.dumps({
                    "scenes": [scene.to_dict() for scene in project.scenes_ordered()]
                }, ensure_ascii=False)

            body = await asyncio.to_thread(load_and_serialize_scenes)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        return web.Response(text=body, content_type="application/json")

    @routes.post("/sonder-editor/project/{project_id}/scenes")
    async def api_create_scene(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        scene = Scene(
            name=body.get("name", "Untitled Scene"),
            duration_frames=body.get("duration_frames", 200),
            prompt=body.get("prompt", ""),
        )
        project.add_scene(scene)
        save_project(project)

        return web.json_response(scene.to_dict(), status=201)

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/duplicate")
    async def api_duplicate_scene(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        source_scene = project.get_scene(request.match_info["scene_id"])
        if not source_scene:
            return _json_error(f"Scene not found: {request.match_info['scene_id']}", 404)

        payload = source_scene.to_dict()
        payload.pop("scene_id", None)
        for clip_payload in payload.get("clips", []) or []:
            if isinstance(clip_payload, dict):
                clip_payload.pop("clip_id", None)
        for track_payload in payload.get("audio_tracks", []) or []:
            if isinstance(track_payload, dict):
                track_payload.pop("track_id", None)

        existing_names = {scene.name for scene in project.scenes}
        copy_base = f"{source_scene.name} (copy)"
        copy_name = copy_base
        suffix = 2
        while copy_name in existing_names:
            copy_name = f"{copy_base} {suffix}"
            suffix += 1
        payload["name"] = copy_name
        payload["order"] = max((getattr(scene, "order", 0) for scene in project.scenes), default=-1) + 1

        new_scene = Scene.from_dict(payload)
        project.add_scene(new_scene)
        save_project(project)
        return web.json_response(new_scene.to_dict(), status=201)

    @routes.get("/sonder-editor/project/{project_id}/scenes/{scene_id}")
    async def api_get_scene(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        return web.json_response(scene.to_dict())

    @routes.put("/sonder-editor/project/{project_id}/scenes/{scene_id}")
    async def api_update_scene(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        old_effective_fps = effective_scene_fps(project, scene)

        if "name" in body:
            scene.name = body["name"]
        if "duration_frames" in body:
            scene.duration_frames = int(body["duration_frames"])
        if "prompt" in body:
            if getattr(scene.global_prompt_track_config, "locked", False):
                return _json_error("Global prompt track is locked", 409)
            scene.prompt = body["prompt"]
        if "prompt_sections" in body:
            scene.prompt_sections = [
                PromptSection.from_dict(p) for p in body["prompt_sections"]
            ]
        if "generation_params" in body:
            scene.generation_params = body["generation_params"]
        if "video_lane_count" in body:
            scene.video_lane_count = max(1, int(body["video_lane_count"]))
        if "motion_driver_lane_count" in body:
            scene.motion_driver_lane_count = max(1, int(body["motion_driver_lane_count"]))
        if "audio_lane_count" in body:
            scene.audio_lane_count = max(1, int(body["audio_lane_count"]))
        if "video_lane_configs" in body:
            scene.video_lane_configs = [
                LaneConfig.from_dict(c) for c in body["video_lane_configs"]
            ]
        if "motion_driver_lane_configs" in body:
            scene.motion_driver_lane_configs = [
                LaneConfig.from_dict(c) for c in body["motion_driver_lane_configs"]
            ]
        if "audio_lane_configs" in body:
            scene.audio_lane_configs = [
                LaneConfig.from_dict(c) for c in body["audio_lane_configs"]
            ]
        if "guide_track_config" in body:
            scene.guide_track_config = LaneConfig.from_dict(body["guide_track_config"])
        if "prompt_track_config" in body:
            scene.prompt_track_config = LaneConfig.from_dict(body["prompt_track_config"])
        if "global_prompt_track_config" in body:
            scene.global_prompt_track_config = LaneConfig.from_dict(body["global_prompt_track_config"])
        if "width" in body:
            scene.width = int(body["width"])
        if "height" in body:
            scene.height = int(body["height"])
        if "fps" in body:
            new_scene_fps = max(0.0, float(body["fps"]))
            previous_scene_fps = scene.fps
            scene.fps = new_scene_fps
            new_effective_fps = effective_scene_fps(project, scene)
            scene.fps = previous_scene_fps
            try:
                if new_effective_fps != old_effective_fps:
                    _require_scene_queue_idle(project, scene.scene_id)
                    retime_scene_geometry(scene, old_effective_fps, new_effective_fps)
            except ProjectMutationRequestError as e:
                return _mutation_json_error(e)
            scene.fps = new_scene_fps
        # Auto-pad configs to match lane counts
        while len(scene.video_lane_configs) < scene.video_lane_count:
            scene.video_lane_configs.append(LaneConfig())
        while len(scene.motion_driver_lane_configs) < scene.motion_driver_lane_count:
            scene.motion_driver_lane_configs.append(LaneConfig())
        while len(scene.audio_lane_configs) < scene.audio_lane_count:
            scene.audio_lane_configs.append(LaneConfig())

        try:
            _validate_single_driver_per_lane(scene)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        save_project(project)
        return web.json_response(scene.to_dict())

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/mutations")
    async def api_apply_scene_mutations(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        operations = body.get("operations", [])
        if not isinstance(operations, list):
            return _json_error("operations must be a list", 400)

        try:
            project, payload = await asyncio.to_thread(
                _apply_scene_mutations_sync,
                request,
                request.match_info["scene_id"],
                operations,
            )
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ProjectMutationRequestError as exc:
            return _mutation_json_error(exc)

        _remember_request_project(request, project)
        return web.json_response(payload)

    @routes.delete("/sonder-editor/project/{project_id}/scenes/{scene_id}")
    async def api_delete_scene(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        if not project.remove_scene(scene_id):
            return _json_error(f"Scene not found: {scene_id}", 404)

        save_project(project)
        return web.json_response({"status": "deleted"})

    # -----------------------------------------------------------------------
    # Scene restore (undo/redo support)
    # -----------------------------------------------------------------------

    @routes.put("/sonder-editor/project/{project_id}/scenes/{scene_id}/restore")
    async def api_restore_scene(request: web.Request) -> web.Response:
        """Replace a scene entirely from a snapshot (for undo/redo)."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        # Restore all mutable scene fields from the snapshot
        scene.name = body.get("name", scene.name)
        scene.duration_frames = body.get("duration_frames", scene.duration_frames)
        scene.prompt = body.get("prompt", scene.prompt)

        if "prompt_sections" in body:
            scene.prompt_sections = [
                PromptSection.from_dict(p) for p in body["prompt_sections"]
            ]
        if "guide_frames" in body:
            scene.guide_frames = [
                GuideFrame.from_dict(g) for g in body["guide_frames"]
            ]
        if "clips" in body:
            scene.clips = [
                ClipReference.from_dict(c) for c in body["clips"]
            ]
        if "audio_tracks" in body:
            scene.audio_tracks = [
                AudioTrack.from_dict(a) for a in body["audio_tracks"]
            ]
        if "video_lane_count" in body:
            scene.video_lane_count = max(1, int(body["video_lane_count"]))
        if "motion_driver_lane_count" in body:
            scene.motion_driver_lane_count = max(1, int(body["motion_driver_lane_count"]))
        if "audio_lane_count" in body:
            scene.audio_lane_count = max(1, int(body["audio_lane_count"]))
        if "video_lane_configs" in body:
            scene.video_lane_configs = [
                LaneConfig.from_dict(c) for c in body["video_lane_configs"]
            ]
        if "motion_driver_lane_configs" in body:
            scene.motion_driver_lane_configs = [
                LaneConfig.from_dict(c) for c in body["motion_driver_lane_configs"]
            ]
        if "audio_lane_configs" in body:
            scene.audio_lane_configs = [
                LaneConfig.from_dict(c) for c in body["audio_lane_configs"]
            ]
        if "guide_track_config" in body:
            scene.guide_track_config = LaneConfig.from_dict(body["guide_track_config"])
        if "prompt_track_config" in body:
            scene.prompt_track_config = LaneConfig.from_dict(body["prompt_track_config"])
        while len(scene.video_lane_configs) < scene.video_lane_count:
            scene.video_lane_configs.append(LaneConfig())
        while len(scene.motion_driver_lane_configs) < scene.motion_driver_lane_count:
            scene.motion_driver_lane_configs.append(LaneConfig())
        while len(scene.audio_lane_configs) < scene.audio_lane_count:
            scene.audio_lane_configs.append(LaneConfig())

        try:
            _validate_single_driver_per_lane(scene)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        save_project(project)
        return web.json_response(scene.to_dict())

    # -----------------------------------------------------------------------
    # Guide frames
    # -----------------------------------------------------------------------

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/guides")
    async def api_add_guide(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)
        if getattr(scene.guide_track_config, "locked", False):
            return _json_error("Guide track is locked", 409)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        try:
            guide = _apply_create_guide(scene, body)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        save_project(project)
        return web.json_response(guide.to_dict(), status=201)

    @routes.get("/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-guides")
    async def api_bridge_guides(request: web.Request) -> web.Response:
        """Return every guide the Sonder Guides Bridge would inject for this scene.

        - If an in-flight queue job exists for this scene with `snapshot_version > 0`,
          read guides from that job's snapshot. Else read live `scene.guide_frames`.
        - Window is the full scene (`[0, duration_frames)`); legacy `selection_start`,
          `selection_end`, `pre_context`, `post_context` query params are ignored so
          the panel can show all scene guides for cross-batch planning.
        - Rows are tagged with `editor_muted` and `source`; `all_guide_keys` carries
          the full scene-level key set so the frontend can prune stale per-guide
          overrides against the scene, not against the rendered window.
        """
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        duration = max(0, int(getattr(scene, "duration_frames", 0) or 0))
        window_start = 0
        window_end = duration

        # Pick snapshot from a running job for this scene if one exists.
        active_job = None
        for job in getattr(project, "generation_queue", []) or []:
            if getattr(job, "scene_id", "") != scene_id:
                continue
            status = str(getattr(job, "status", "") or "").lower()
            if status != "running":
                continue
            params = getattr(job, "params", {}) or {}
            try:
                snap_ver = int(params.get("snapshot_version", 0) or 0) if isinstance(params, dict) else 0
            except (TypeError, ValueError):
                snap_ver = 0
            if snap_ver > 0:
                active_job = job
                break

        if active_job is not None:
            guides_src = [
                GuideFrame.from_dict(g)
                for g in getattr(active_job, "guide_frame_snapshots", []) or []
                if isinstance(g, dict)
            ]
            source_label = "snapshot"
        else:
            guides_src = list(getattr(scene, "guide_frames", []) or [])
            source_label = "live"
        live_guide_track_hidden = source_label == "live" and bool(getattr(scene.guide_track_config, "hidden", False))

        rows = []
        all_guide_keys = []
        for guide in guides_src:
            try:
                idx = int(getattr(guide, "frame_index", 0))
            except (TypeError, ValueError):
                continue
            if idx == -1:
                idx = max(0, duration - 1)
            asset_id = getattr(guide, "asset_id", "")
            all_guide_keys.append(f"{asset_id}:{idx}")
            if not (window_start <= idx < window_end):
                continue
            asset = project.get_asset(asset_id)
            asset_name = ""
            if asset is not None:
                asset_name = getattr(asset, "name", "") or ""
                if not asset_name:
                    path = getattr(asset, "path", "") or ""
                    asset_name = path.replace("\\", "/").split("/")[-1] if path else asset_id
            rows.append({
                "guide_key": f"{asset_id}:{idx}",
                "frame_index": idx,
                "asset_id": asset_id,
                "asset_name": asset_name or asset_id or "Guide",
                "strength": float(getattr(guide, "strength", 1.0) or 0.0),
                "editor_muted": live_guide_track_hidden or bool(getattr(guide, "muted", False)),
                "source": source_label,
            })

        rows.sort(key=lambda r: r["frame_index"])
        return web.json_response({
            "window_start": window_start,
            "window_end": window_end,
            "scene_name": getattr(scene, "name", "") or scene_id,
            "source": source_label,
            "guides": rows,
            "all_guide_keys": all_guide_keys,
        })

    @routes.get("/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-drivers")
    async def api_bridge_drivers(request: web.Request) -> web.Response:
        """Return Driver lane state for the Sonder Driver Bridge panel.

        This panel is informational. Execution remains authoritative through
        the project._execution_context queue_job_ref_id consumed by the node.
        """
        try:
            project = await asyncio.to_thread(_load_project_from_request, request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        active_job = None
        for job in getattr(project, "generation_queue", []) or []:
            if getattr(job, "scene_id", "") != scene_id:
                continue
            if str(getattr(job, "status", "") or "").lower() != "running":
                continue
            params = getattr(job, "params", {}) or {}
            try:
                snap_ver = int(params.get("snapshot_version", 0) or 0) if isinstance(params, dict) else 0
            except (TypeError, ValueError):
                snap_ver = 0
            if snap_ver > 0:
                active_job = job
                break

        if active_job is not None:
            lane_count = max(1, int(getattr(active_job, "driver_lane_count", 1) or 1))
            lane_configs = [
                LaneConfig.from_dict(item) if isinstance(item, dict) else item
                for item in (getattr(active_job, "driver_lane_configs", []) or [])
            ]
            clips = [
                ClipReference.from_dict(item)
                for item in (getattr(active_job, "driver_clip_snapshots", []) or [])
                if isinstance(item, dict)
            ]
            source_label = "snapshot"
        else:
            lane_count = max(1, int(getattr(scene, "motion_driver_lane_count", 1) or 1))
            lane_configs = list(getattr(scene, "motion_driver_lane_configs", []) or [])
            clips = list(getattr(scene, "clips", []) or [])
            source_label = "live"

        while len(lane_configs) < lane_count:
            lane_configs.append(LaneConfig())

        def _asset_name_for_source(source_path: str) -> str:
            norm = str(source_path or "").replace("\\", "/")
            for asset in getattr(project, "assets", []) or []:
                if str(getattr(asset, "path", "") or "").replace("\\", "/") == norm:
                    return getattr(asset, "name", "") or os.path.basename(norm)
            return os.path.basename(norm) if norm else ""

        rows = []
        all_driver_keys = []
        for lane_index in range(lane_count):
            cfg = lane_configs[lane_index]
            lane_name = getattr(cfg, "name", "") or f"Driver {lane_index + 1}"
            hidden = bool(getattr(cfg, "hidden", False))
            lane_key = f"lane:{lane_index}"
            all_driver_keys.append(lane_key)
            lane_clips = [
                clip for clip in clips
                if getattr(clip, "role", "render") == "motion_driver"
                and int(getattr(clip, "track_index", 0) or 0) == lane_index
            ]
            lane_clips.sort(key=lambda clip: int(getattr(clip, "timeline_start_frame", 0) or 0))
            clip = lane_clips[0] if lane_clips else None
            source_path = str(getattr(clip, "source_path", "") or "") if clip else ""
            rows.append({
                "lane_key": lane_key,
                "lane_index": lane_index,
                "lane_name": lane_name,
                "hidden": hidden,
                "has_clip": clip is not None,
                "clip_id": getattr(clip, "clip_id", "") if clip else "",
                "asset_name": _asset_name_for_source(source_path) or "Driver",
                "source_path": source_path,
                "start_frame": int(getattr(clip, "timeline_start_frame", 0) or 0) if clip else 0,
                "end_frame": int(getattr(clip, "timeline_end_frame", 0) or 0) if clip else 0,
                "strength": float(getattr(clip, "strength", 0.0) or 0.0) if clip else 0.0,
                "editor_muted": hidden or (bool(getattr(clip, "muted", False)) if clip else False),
                "duplicate_count": max(0, len(lane_clips) - 1),
                "source": source_label,
            })

        return web.json_response({
            "window_start": 0,
            "window_end": max(0, int(getattr(scene, "duration_frames", 0) or 0)),
            "scene_name": getattr(scene, "name", "") or scene_id,
            "source": source_label,
            "driver_lane_count": lane_count,
            "drivers": rows,
            "all_driver_keys": all_driver_keys,
        })

    @routes.get("/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-payload")
    async def api_prompt_payload(request: web.Request) -> web.Response:
        """Resolved prompt segments + PromptRelay payload preview for this scene.

        - Snapshot-vs-live mirrors `bridge-guides`: a running snapshot_version>0
          job for the scene wins, else live scene state with per-lane hidden
          composition and the project-durable labels toggle.
        - Window is the FULL scene (`[0, duration_frames)`), so this is a
          structural preview: execution payloads rebase tags/lengths to the
          render window and will not textually match this response.
        """
        try:
            # Project load is filesystem work — keep it off the event loop.
            project = await asyncio.to_thread(_load_project_from_request, request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        duration = max(0, int(getattr(scene, "duration_frames", 0) or 0))

        active_job = None
        for job in getattr(project, "generation_queue", []) or []:
            if getattr(job, "scene_id", "") != scene_id:
                continue
            if str(getattr(job, "status", "") or "").lower() != "running":
                continue
            params = getattr(job, "params", {}) or {}
            try:
                snap_ver = int(params.get("snapshot_version", 0) or 0) if isinstance(params, dict) else 0
            except (TypeError, ValueError):
                snap_ver = 0
            if snap_ver > 0:
                active_job = job
                break

        def _coerce_threshold(source) -> float:
            if not isinstance(source, dict):
                return 10.0
            try:
                return float(source.get("prompt_frame_threshold", 10.0) or 0.0)
            except (TypeError, ValueError):
                return 10.0

        if active_job is not None:
            params = getattr(active_job, "params", {}) or {}
            labels_on = params.get("prompt_channel_labels", False) is True \
                if isinstance(params, dict) else False
            threshold = _coerce_threshold(params)
            global_text = str(getattr(active_job, "scene_prompt", "") or "")
            sections = list(getattr(active_job, "prompt_sections", []) or [])
            source_label = "snapshot"
        else:
            metadata = getattr(project, "metadata", None)
            labels_on = metadata.get("prompt_channel_labels", False) is True \
                if isinstance(metadata, dict) else False
            threshold = _coerce_threshold(metadata)
            global_hidden = bool(getattr(scene.global_prompt_track_config, "hidden", False))
            sections_hidden = bool(getattr(scene.prompt_track_config, "hidden", False))
            global_text = "" if global_hidden else (scene.prompt or "")
            sections = [] if sections_hidden else list(scene.prompt_sections or [])
            source_label = "live"

        # Optional selection-scoped window for the timeline highlight; default
        # is the full scene so the structural panel preview is unchanged.
        def _q_int(name, default):
            raw = request.query.get(name)
            if raw is None or raw == "":
                return default
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default
        window_start = max(0, _q_int("window_start", 0))
        window_end = min(duration, _q_int("window_end", duration))
        if window_end <= window_start:
            window_start, window_end = 0, duration

        segments = prompt_payload.resolve_segments(
            sections, window_start, window_end, labels_on, threshold)
        relay = prompt_payload.build_relay_payload(global_text, segments)

        # Which authored sections survive (used) vs were dropped by the
        # boundary threshold for this window — drives the highlight. Diffing a
        # threshold=0 resolve isolates exactly the spill drops.
        used_sections = sorted({int(s["section_start"]) for s in segments
                                if "section_start" in s})
        candidates = prompt_payload.resolve_segments(
            sections, window_start, window_end, labels_on, 0.0)
        candidate_keys = {int(s["section_start"]) for s in candidates
                          if "section_start" in s}
        dropped_sections = sorted(candidate_keys.difference(used_sections))

        return web.json_response({
            "window_start": window_start,
            "window_end": window_end,
            "scene_name": getattr(scene, "name", "") or scene_id,
            "source": source_label,
            "labels_on": labels_on,
            "global_prompt": relay["global_prompt"],
            "segments": relay["segments"],
            "used_sections": used_sections,
            "dropped_sections": dropped_sections,
            "relay": {
                "global_prompt": relay["global_prompt"],
                "smart_prompt": relay["smart_prompt"],
                "local_prompts": relay["local_prompts"],
                "segment_lengths": relay["segment_lengths"],
            },
        })

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/guides/swap")
    async def api_swap_guides(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)
        if getattr(scene.guide_track_config, "locked", False):
            return _json_error("Guide track is locked", 409)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        try:
            frame_a = int(body.get("frame_a"))
            frame_b = int(body.get("frame_b"))
        except (TypeError, ValueError):
            return _json_error("Invalid guide frame index", 400)

        if frame_a == frame_b:
            guide = next((g for g in scene.guide_frames if g.frame_index == frame_a), None)
            if not guide:
                return _json_error(f"No guide at frame {frame_a}", 404)
            return web.json_response({"guides": [guide.to_dict()]})

        guide_a = next((g for g in scene.guide_frames if g.frame_index == frame_a), None)
        guide_b = next((g for g in scene.guide_frames if g.frame_index == frame_b), None)
        if not guide_a:
            return _json_error(f"No guide at frame {frame_a}", 404)
        if not guide_b:
            return _json_error(f"No guide at frame {frame_b}", 404)

        guide_a.frame_index, guide_b.frame_index = frame_b, frame_a
        scene.guide_frames.sort(key=lambda g: g.frame_index)
        save_project(project)
        return web.json_response({"guides": [guide_a.to_dict(), guide_b.to_dict()]})

    @routes.patch("/sonder-editor/project/{project_id}/scenes/{scene_id}/guides/{frame_index}")
    async def api_update_guide(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)
        if getattr(scene.guide_track_config, "locked", False):
            return _json_error("Guide track is locked", 409)

        try:
            frame_index = int(request.match_info["frame_index"])
        except (TypeError, ValueError):
            return _json_error("Invalid guide frame index", 400)

        guide = next((g for g in scene.guide_frames if g.frame_index == frame_index), None)
        if not guide:
            return _json_error(f"No guide at frame {frame_index}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        try:
            guide = _apply_update_guide(scene, frame_index, body)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        save_project(project)
        return web.json_response(guide.to_dict())

    @routes.delete("/sonder-editor/project/{project_id}/scenes/{scene_id}/guides/{frame_index}")
    async def api_delete_guide(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)
        if getattr(scene.guide_track_config, "locked", False):
            return _json_error("Guide track is locked", 409)

        frame_index = int(request.match_info["frame_index"])
        try:
            _apply_delete_guide(scene, frame_index)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        save_project(project)
        return web.json_response({"status": "deleted"})

    # -----------------------------------------------------------------------
    # Clips (video on timeline)
    # -----------------------------------------------------------------------

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/clips")
    async def api_add_clip(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        asset_id = body.get("asset_id", "")
        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        role = body.get("role", "render")
        if role not in {"render", "motion_driver"}:
            return _json_error(f"Invalid clip role: {role}", 400)
        if role == "motion_driver" and asset.asset_type != "video":
            return _json_error("Driver clips require video assets", 400)
        start_frame = int(body.get("timeline_start_frame", 0))
        native_frame_count = _valid_source_frame_count(asset)
        frame_count = media_timeline_frames(asset, effective_scene_fps(project, scene))
        if asset.asset_type == "video" and native_frame_count <= 0 and not _finite_positive_number(getattr(asset, "duration_sec", 0.0)):
            return _json_error("Video asset has invalid duration metadata. Refresh assets or re-import the file.", 400)
        frame_count = frame_count or 1
        end_frame = start_frame + frame_count
        track_index = int(body.get("track_index", 0))
        audio_lane_idx = int(body.get("audio_lane_index", 0))
        clip_fit_mode = str(body.get("fit_mode", DEFAULT_FIT_MODE) or DEFAULT_FIT_MODE).strip().lower()
        if clip_fit_mode not in FIT_MODES:
            return _json_error(f"Invalid fit_mode: {clip_fit_mode}", 400)
        clip_crop_position = str(body.get("crop_position", DEFAULT_CROP_POSITION) or DEFAULT_CROP_POSITION).strip().lower()
        if clip_crop_position not in CROP_POSITIONS:
            return _json_error(f"Invalid crop_position: {clip_crop_position}", 400)
        try:
            _require_lane_unlocked(scene, "motion_driver" if role == "motion_driver" else "video", track_index)
            if role != "motion_driver" and body.get("dual_drop") and asset.asset_type == "video" and asset.has_audio:
                _require_lane_unlocked(scene, "audio", audio_lane_idx)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        clip = ClipReference(
            source_path=asset.path,
            timeline_start_frame=start_frame,
            timeline_end_frame=end_frame,
            source_in_frame=0,
            source_out_frame=frame_count,
            total_source_frames=frame_count,
            track_index=track_index,
            role=role,
            strength=float(body.get("strength", 1.0)),
            muted=bool(body.get("muted", False)),
            fit_mode=clip_fit_mode,
            crop_position=clip_crop_position,
        )
        scene.clips.append(clip)
        try:
            _validate_single_driver_per_lane(scene)
        except ProjectMutationRequestError as e:
            scene.clips = [existing for existing in scene.clips if existing is not clip]
            return _mutation_json_error(e)

        # Dual drop: also create audio track if video has audio
        # Wrapped in try/except so audio extraction failure doesn't prevent clip creation
        audio_track_dict = None
        if role != "motion_driver" and body.get("dual_drop") and asset.asset_type == "video" and asset.has_audio:
            try:
                audio_asset = await asyncio.to_thread(_prepare_video_audio_asset, project, asset)
                if audio_asset:
                    if _valid_audio_duration_frames(audio_asset, effective_scene_fps(project, scene)) <= 0:
                        raise ValueError("Extracted audio has invalid duration metadata")
                    audio_frames = frame_count
                    audio_track = AudioTrack(
                        source_path=audio_asset.path,
                        timeline_start_frame=start_frame,
                        timeline_end_frame=start_frame + audio_frames,
                        total_source_frames=audio_frames,
                        lane_index=audio_lane_idx,
                    )
                    scene.audio_tracks.append(audio_track)
                    audio_track_dict = audio_track.to_dict()
                    if body.get("linked") is not False and body.get("link_video_audio", True):
                        _add_link_group(scene, [
                            {"type": "clip", "id": clip.clip_id},
                            {"type": "audio", "id": audio_track.track_id},
                        ])
            except Exception as e:
                logger.warning("Dual drop audio extraction failed: %s", e)

        save_project(project)

        result = clip.to_dict()
        if audio_track_dict:
            result["audio_track"] = audio_track_dict
        return web.json_response(result, status=201)

    @routes.delete("/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}")
    async def api_delete_clip(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        clip_id = request.match_info["clip_id"]
        clip = next((c for c in scene.clips if c.clip_id == clip_id), None)
        if not clip:
            return _json_error(f"Clip not found: {clip_id}", 404)
        try:
            _require_clip_unlocked(scene, clip)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        deleted_lane = clip.track_index or 0
        should_compact_lane = _is_render_clip(clip) and request.query.get("preserve_lane") != "1"
        original_count = len(scene.clips)
        scene.clips = [c for c in scene.clips if c.clip_id != clip_id]

        if len(scene.clips) == original_count:
            return _json_error(f"Clip not found: {clip_id}", 404)

        if should_compact_lane:
            _compact_empty_media_lane(scene, "video", deleted_lane)

        save_project(project)
        return web.json_response({"status": "deleted"})

    @routes.put("/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}")
    async def api_update_clip(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        clip_id = request.match_info["clip_id"]
        clip = next((c for c in scene.clips if c.clip_id == clip_id), None)
        if not clip:
            return _json_error(f"Clip not found: {clip_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)
        try:
            clip = _apply_update_clip(project, scene, clip_id, body)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        save_project(project)
        return web.json_response(clip.to_dict())

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}/split")
    async def api_split_clip(request: web.Request) -> web.Response:
        """Split a clip at a given frame into two clips."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        clip_id = request.match_info["clip_id"]
        clip = next((c for c in scene.clips if c.clip_id == clip_id), None)
        if not clip:
            return _json_error(f"Clip not found: {clip_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        split_frame = int(body.get("frame", 0))
        try:
            result = _apply_split_linked(
                scene,
                _link_ref("clip", clip_id),
                split_frame,
                bool(body.get("apply_linked", False)),
            )
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)
        save_project(project)

        if body.get("apply_linked"):
            return web.json_response({"scene": scene.to_dict(), **result})
        right_ref = result.get("right_items", [None])[0]
        right_clip = _find_clip(scene, right_ref["id"]) if right_ref else None
        return web.json_response({"left": clip.to_dict(), "right": right_clip.to_dict() if right_clip else None})

    # -----------------------------------------------------------------------
    # Audio tracks (audio on timeline)
    # -----------------------------------------------------------------------

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/audio_tracks")
    async def api_add_audio_track(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        asset_id = body.get("asset_id", "")
        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        lane_index = int(body.get("lane_index", 0))
        try:
            _require_lane_unlocked(scene, "audio", lane_index)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        # Zone-model audio-only drop: a video asset on an audio lane places its
        # extracted audio (derived audio asset, deduped by path).
        if asset.asset_type == "video":
            if not asset.has_audio:
                return _json_error("Video has no embedded audio", 400)
            try:
                audio_asset = await asyncio.to_thread(_prepare_video_audio_asset, project, asset)
            except Exception as e:
                logger.warning("Audio-only drop extraction failed for %s: %s", asset_id, e)
                audio_asset = None
            if not audio_asset:
                return _json_error("Failed to extract audio from video", 500)
            asset = audio_asset

        start_frame = int(body.get("timeline_start_frame", 0))
        # Calculate duration in frames from asset duration
        fps = effective_scene_fps(project, scene)
        duration_frames = _valid_audio_duration_frames(asset, fps)
        if asset.asset_type != "audio" or duration_frames <= 0:
            return _json_error("Audio asset has invalid duration metadata. Refresh assets or re-import the file.", 400)
        end_frame = start_frame + duration_frames

        track = AudioTrack(
            source_path=asset.path,
            timeline_start_frame=start_frame,
            timeline_end_frame=end_frame,
            total_source_frames=duration_frames,
            lane_index=lane_index,
        )
        scene.audio_tracks.append(track)
        save_project(project)

        return web.json_response(track.to_dict(), status=201)

    @routes.delete("/sonder-editor/project/{project_id}/scenes/{scene_id}/audio_tracks/{track_id}")
    async def api_delete_audio_track(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        track_id = request.match_info["track_id"]
        track = next((t for t in scene.audio_tracks if t.track_id == track_id), None)
        if not track:
            return _json_error(f"Audio track not found: {track_id}", 404)
        try:
            _require_audio_unlocked(scene, track)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        deleted_lane = track.lane_index or 0
        should_compact_lane = request.query.get("preserve_lane") != "1"
        original_count = len(scene.audio_tracks)
        scene.audio_tracks = [t for t in scene.audio_tracks if t.track_id != track_id]

        if len(scene.audio_tracks) == original_count:
            return _json_error(f"Audio track not found: {track_id}", 404)

        if should_compact_lane:
            _compact_empty_media_lane(scene, "audio", deleted_lane)

        save_project(project)
        return web.json_response({"status": "deleted"})

    @routes.put("/sonder-editor/project/{project_id}/scenes/{scene_id}/audio_tracks/{track_id}")
    async def api_update_audio_track(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        track_id = request.match_info["track_id"]
        track = next((t for t in scene.audio_tracks if t.track_id == track_id), None)
        if not track:
            return _json_error(f"Audio track not found: {track_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)
        try:
            _require_audio_unlocked(scene, track)
            if "lane_index" in body:
                _require_lane_unlocked(scene, "audio", int(body["lane_index"]))
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)

        if "timeline_start_frame" in body:
            new_start = int(body["timeline_start_frame"])
            if "timeline_end_frame" not in body:
                # Move: preserve duration
                duration = track.timeline_end_frame - track.timeline_start_frame
                track.timeline_start_frame = max(0, new_start)
                track.timeline_end_frame = track.timeline_start_frame + duration
            else:
                track.timeline_start_frame = max(0, new_start)

        if "timeline_end_frame" in body:
            track.timeline_end_frame = int(body["timeline_end_frame"])

        if "source_in_frame" in body:
            track.source_in_frame = int(body["source_in_frame"])

        if "muted" in body:
            track.muted = bool(body["muted"])

        if "volume" in body:
            track.volume = float(body["volume"])
        if "lane_index" in body:
            track.lane_index = int(body["lane_index"])

        save_project(project)
        return web.json_response(track.to_dict())

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/audio_tracks/{track_id}/split")
    async def api_split_audio_track(request: web.Request) -> web.Response:
        """Split an audio track at a given frame into two tracks."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)

        track_id = request.match_info["track_id"]
        track = next((t for t in scene.audio_tracks if t.track_id == track_id), None)
        if not track:
            return _json_error(f"Audio track not found: {track_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        split_frame = int(body.get("frame", 0))
        try:
            result = _apply_split_linked(
                scene,
                _link_ref("audio", track_id),
                split_frame,
                bool(body.get("apply_linked", False)),
            )
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)
        save_project(project)

        if body.get("apply_linked"):
            return web.json_response({"scene": scene.to_dict(), **result})
        right_ref = result.get("right_items", [None])[0]
        right_track = _find_audio_track(scene, right_ref["id"]) if right_ref else None
        return web.json_response({"left": track.to_dict(), "right": right_track.to_dict() if right_track else None})

    # -----------------------------------------------------------------------
    # Prompt sections
    # -----------------------------------------------------------------------

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt_sections")
    async def api_add_prompt_section(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)
        if getattr(scene.prompt_track_config, "locked", False):
            return _json_error("Prompt track is locked", 409)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        start_frame = int(body.get("start_frame", 0))
        end_frame = int(body.get("end_frame", 0))
        if end_frame <= start_frame:
            return _json_error("Prompt section range is invalid", 400)
        overlapping = any(
            other.start_frame < end_frame and other.end_frame > start_frame
            for other in scene.prompt_sections
        )
        if overlapping:
            return _json_error("Prompt sections cannot overlap", 409)
        try:
            section = _apply_create_prompt_section(scene, body)
        except ProjectMutationRequestError as e:
            return _mutation_json_error(e)
        save_project(project)

        return web.json_response(section.to_dict(), status=201)

    @routes.put("/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt_sections/{index}")
    async def api_update_prompt_section(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)
        if getattr(scene.prompt_track_config, "locked", False):
            return _json_error("Prompt track is locked", 409)

        idx = int(request.match_info["index"])
        if idx < 0 or idx >= len(scene.prompt_sections):
            return _json_error(f"Prompt section index out of range: {idx}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        section = scene.prompt_sections[idx]
        range_changed = "start_frame" in body or "end_frame" in body
        new_start = int(body["start_frame"]) if "start_frame" in body else section.start_frame
        new_end = int(body["end_frame"]) if "end_frame" in body else section.end_frame
        if range_changed:
            if new_end <= new_start:
                return _json_error("Prompt section range is invalid", 400)
            overlapping = any(
                other is not section
                and other.start_frame < new_end and other.end_frame > new_start
                for other in scene.prompt_sections
            )
            if overlapping:
                return _json_error("Prompt sections cannot overlap", 409)
        section.start_frame = new_start
        section.end_frame = new_end
        raw_channels = body.get("channels")
        if isinstance(raw_channels, dict):
            section.channels = prompt_payload.normalize_channels(raw_channels)
        elif "prompt" in body:
            section.prompt = body["prompt"]
        if "muted" in body:
            section.muted = bool(body["muted"])

        scene.prompt_sections.sort(key=lambda s: s.start_frame)
        save_project(project)

        return web.json_response(section.to_dict())

    @routes.delete("/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt_sections/{index}")
    async def api_delete_prompt_section(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene_id = request.match_info["scene_id"]
        scene = project.get_scene(scene_id)
        if not scene:
            return _json_error(f"Scene not found: {scene_id}", 404)
        if getattr(scene.prompt_track_config, "locked", False):
            return _json_error("Prompt track is locked", 409)

        idx = int(request.match_info["index"])
        if idx < 0 or idx >= len(scene.prompt_sections):
            return _json_error(f"Prompt section index out of range: {idx}", 404)

        prompt_id = getattr(scene.prompt_sections[idx], "prompt_id", "")
        scene.prompt_sections.pop(idx)
        _rewrite_link_groups_for_deleted(scene, [_link_ref("prompt", prompt_id)])
        save_project(project)

        return web.json_response({"status": "deleted"})

    # -----------------------------------------------------------------------
    # Saved selections
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/project/{project_id}/scenes/{scene_id}/saved_selections")
    async def api_list_saved_selections(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene = project.get_scene(request.match_info["scene_id"])
        if not scene:
            return _json_error("Scene not found", 404)

        return web.json_response(scene.saved_selections)

    @routes.post("/sonder-editor/project/{project_id}/scenes/{scene_id}/saved_selections")
    async def api_add_saved_selection(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene = project.get_scene(request.match_info["scene_id"])
        if not scene:
            return _json_error("Scene not found", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        entry = {
            "name": body.get("name", f"Selection {len(scene.saved_selections) + 1}"),
            "start": _coerce_nonnegative_int(body.get("start", 0)),
            "end": _coerce_nonnegative_int(body.get("end", 0)),
            "pre_context_frames": _coerce_nonnegative_int(body.get("pre_context_frames", 0)),
            "post_context_frames": _coerce_nonnegative_int(body.get("post_context_frames", 0)),
            "mask_pre_offset": _coerce_nonnegative_int(body.get("mask_pre_offset", 0)),
            "mask_post_offset": _coerce_nonnegative_int(body.get("mask_post_offset", 0)),
        }
        scene.saved_selections.append(entry)
        save_project(project)
        return web.json_response({"index": len(scene.saved_selections) - 1, "entry": entry})

    @routes.put("/sonder-editor/project/{project_id}/scenes/{scene_id}/saved_selections/{index}")
    async def api_update_saved_selection(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene = project.get_scene(request.match_info["scene_id"])
        if not scene:
            return _json_error("Scene not found", 404)

        idx = int(request.match_info["index"])
        if idx < 0 or idx >= len(scene.saved_selections):
            return _json_error("Selection index out of range", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        if "name" in body:
            scene.saved_selections[idx]["name"] = body["name"]
        if "start" in body:
            scene.saved_selections[idx]["start"] = _coerce_nonnegative_int(body["start"])
        if "end" in body:
            scene.saved_selections[idx]["end"] = _coerce_nonnegative_int(body["end"])
        if "pre_context_frames" in body:
            scene.saved_selections[idx]["pre_context_frames"] = _coerce_nonnegative_int(body["pre_context_frames"])
        if "post_context_frames" in body:
            scene.saved_selections[idx]["post_context_frames"] = _coerce_nonnegative_int(body["post_context_frames"])
        if "mask_pre_offset" in body:
            scene.saved_selections[idx]["mask_pre_offset"] = _coerce_nonnegative_int(body["mask_pre_offset"])
        if "mask_post_offset" in body:
            scene.saved_selections[idx]["mask_post_offset"] = _coerce_nonnegative_int(body["mask_post_offset"])

        save_project(project)
        return web.json_response(scene.saved_selections[idx])

    @routes.delete("/sonder-editor/project/{project_id}/scenes/{scene_id}/saved_selections/{index}")
    async def api_delete_saved_selection(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        scene = project.get_scene(request.match_info["scene_id"])
        if not scene:
            return _json_error("Scene not found", 404)

        idx = int(request.match_info["index"])
        if idx < 0 or idx >= len(scene.saved_selections):
            return _json_error("Selection index out of range", 404)

        scene.saved_selections.pop(idx)
        save_project(project)
        return web.json_response({"status": "deleted"})

    # -----------------------------------------------------------------------
    # Render queue
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/project/{project_id}/queue")
    async def api_list_queue(request: web.Request) -> web.Response:
        try:
            def load_and_serialize_queue() -> str:
                project = _load_project_from_request(request)
                return json.dumps([job.to_dict() for job in project.generation_queue], ensure_ascii=False)

            body = await asyncio.to_thread(load_and_serialize_queue)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        return web.Response(text=body, content_type="application/json")

    @routes.post("/sonder-editor/project/{project_id}/queue")
    async def api_add_queue_job(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)
        if not isinstance(body, dict):
            return _json_error("Queue job body must be an object", 400)

        def add_one(project: TimelineProject) -> tuple[bool, dict]:
            job = _queue_job_from_body(body)
            _compose_frozen_job_prompt(project, job)
            _freeze_guide_collision_param(project, job)
            _queue_guide_collision_prediction(project, job)
            project.generation_queue.append(job)
            _record_prompt_history(project, [job])
            return True, job.to_dict()

        try:
            project, payload = await asyncio.to_thread(_apply_queue_versioned_sync, request, add_one)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        _remember_request_project(request, project)
        return web.json_response(payload)

    @routes.post("/sonder-editor/project/{project_id}/queue/batch")
    async def api_add_queue_batch(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)
        if not isinstance(body, dict):
            return _json_error("Queue batch body must be an object", 400)

        raw_jobs = body.get("jobs", [])
        if not isinstance(raw_jobs, list):
            return _json_error("jobs must be a list", 400)
        if not raw_jobs:
            return _json_error("jobs must not be empty", 400)
        if not all(isinstance(item, dict) for item in raw_jobs):
            return _json_error("Each batch job must be an object", 400)

        def add_batch(project: TimelineProject) -> tuple[bool, dict]:
            jobs = [_queue_job_from_body(item) for item in raw_jobs]
            for job in jobs:
                _compose_frozen_job_prompt(project, job)
                _freeze_guide_collision_param(project, job)
                _queue_guide_collision_prediction(project, job)
            project.generation_queue.extend(jobs)
            _record_prompt_history(project, jobs)
            return True, {
                "status": "ok",
                "count": len(jobs),
                "jobs": [job.to_dict() for job in jobs],
            }

        try:
            project, payload = await asyncio.to_thread(_apply_queue_versioned_sync, request, add_batch)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        _remember_request_project(request, project)
        return web.json_response(payload, status=201)

    @routes.post("/sonder-editor/project/{project_id}/queue/mutations")
    async def api_apply_queue_mutations(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)
        if not isinstance(body, dict):
            return _json_error("Queue mutation body must be an object", 400)

        operations = body.get("operations", [])
        if not isinstance(operations, list):
            return _json_error("operations must be a list", 400)

        try:
            project, payload = await asyncio.to_thread(
                _apply_queue_versioned_sync,
                request,
                lambda queue_project: _apply_queue_mutation_operations(queue_project, operations),
            )
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ProjectMutationRequestError as exc:
            return _mutation_json_error(exc)

        _remember_request_project(request, project)
        return web.json_response(payload)

    @routes.put("/sonder-editor/project/{project_id}/queue/{job_id}")
    async def api_update_queue_job(request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)
        if not isinstance(body, dict):
            return _json_error("Queue update body must be an object", 400)

        def update_one(project: TimelineProject) -> tuple[bool, dict | None]:
            job = next((j for j in project.generation_queue if j.job_id == job_id), None)
            if not job:
                return False, None
            changed = False
            if "status" in body:
                job.status = body["status"]
                changed = True
            if "progress" in body:
                job.progress = float(body["progress"])
                changed = True
            if "error" in body:
                job.error = body["error"]
                changed = True
            if "result_asset_id" in body:
                job.result_asset_id = body["result_asset_id"]
                changed = True
            if "completed_at" in body:
                job.completed_at = body["completed_at"]
                changed = True
            return changed, job.to_dict()

        try:
            project, payload = await asyncio.to_thread(_apply_queue_versioned_sync, request, update_one)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        if payload is None:
            return _json_error(f"Job not found: {job_id}", 404)
        _remember_request_project(request, project)
        return web.json_response(payload)

    @routes.delete("/sonder-editor/project/{project_id}/queue/{job_id}")
    async def api_delete_queue_job(request: web.Request) -> web.Response:
        job_id = request.match_info["job_id"]

        def delete_one(project: TimelineProject) -> tuple[bool, dict]:
            changed, payload = _apply_queue_mutation_operations(project, [{"type": "delete_job", "job_id": job_id}])
            removed = int(payload["results"][0].get("removed", 0)) if payload.get("results") else 0
            return changed, {"status": "deleted", "removed": removed, "queue": payload["queue"]}

        try:
            project, payload = await asyncio.to_thread(_apply_queue_versioned_sync, request, delete_one)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ProjectMutationRequestError as exc:
            return _mutation_json_error(exc)
        _remember_request_project(request, project)
        return web.json_response(payload)

    @routes.delete("/sonder-editor/project/{project_id}/queue")
    async def api_clear_queue(request: web.Request) -> web.Response:
        """Clear completed jobs, or all if ?all=1."""
        clear_all = request.query.get("all") == "1"

        def clear_queue(project: TimelineProject) -> tuple[bool, dict]:
            op_type = "clear_all" if clear_all else "clear_completed"
            changed, payload = _apply_queue_mutation_operations(project, [{"type": op_type}])
            removed = int(payload["results"][0].get("removed", 0)) if payload.get("results") else 0
            return changed, {"status": "cleared", "removed": removed, "queue": payload["queue"]}

        try:
            project, payload = await asyncio.to_thread(_apply_queue_versioned_sync, request, clear_queue)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)
        except ProjectMutationRequestError as exc:
            return _mutation_json_error(exc)
        _remember_request_project(request, project)
        return web.json_response(payload)

    # -----------------------------------------------------------------------
    # WebSocket stub
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/ws")
    async def api_websocket(request: web.Request) -> web.StreamResponse:
        if not _same_origin_request(request):
            return _cross_origin_blocked_response(request)
        remember_event_loop()
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # SwarmUI's ComfyBackendDirect proxy needs a Comfy-style sid to close cleanly.
        await ws.send_json({"type": "status", "data": {"sid": uuid.uuid4().hex}})
        subscribed_project_id = str(request.query.get("project_id") or "")
        host_registration: dict[str, str] | None = None

        async def clear_host_registration() -> None:
            nonlocal host_registration
            if not host_registration:
                return
            await unregister_canvas_host(
                host_registration.get("project_id", ""),
                host_registration.get("source_node_id", ""),
                host_registration.get("session_id", ""),
                host_id=host_registration.get("host_id", ""),
            )
            host_registration = None

        if subscribed_project_id:
            await subscribe(subscribed_project_id, ws)
            await ws.send_json({"type": "subscribed", "project_id": subscribed_project_id})

        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        message_type = data.get("type", "unknown")
                        if message_type == "subscribe":
                            next_project_id = str(data.get("project_id") or "")
                            source_node_id = str(data.get("source_node_id") or "")
                            host_id = str(data.get("host_id") or "")
                            await clear_host_registration()
                            if subscribed_project_id:
                                await unsubscribe(subscribed_project_id, ws)
                            subscribed_project_id = next_project_id
                            if subscribed_project_id:
                                await subscribe(subscribed_project_id, ws)

                            response = {"type": "subscribed", "project_id": subscribed_project_id}
                            if subscribed_project_id and data.get("role") == "canvas_host":
                                session_id = str(data.get("session_id") or "")
                                host_result = await register_canvas_host(
                                    subscribed_project_id,
                                    source_node_id,
                                    session_id,
                                    str(data.get("workflow_id") or ""),
                                    str(data.get("workflow_label") or ""),
                                    host_id=host_id,
                                )
                                host_registration = {
                                    "project_id": subscribed_project_id,
                                    "host_id": host_result.get("host_id") or host_id,
                                    "source_node_id": source_node_id,
                                    "session_id": session_id,
                                }
                                response.update({
                                    "host_id": host_result.get("host_id") or host_id,
                                    "source_node_id": host_result.get("source_node_id") or source_node_id,
                                    "host": host_result.get("host"),
                                    "canvas_host_connected": host_result.get("canvas_host_connected", False),
                                })
                            elif subscribed_project_id and (host_id or source_node_id):
                                host = await get_canvas_host(subscribed_project_id, source_node_id, host_id=host_id)
                                response.update({
                                    "host_id": host.get("host_id") if host else host_id or source_node_id,
                                    "source_node_id": host.get("source_node_id") if host else source_node_id,
                                    "host": host,
                                    "canvas_host_connected": bool(host),
                                })
                            await ws.send_json(response)
                        elif message_type == "host_heartbeat":
                            if host_registration:
                                host_result = await refresh_canvas_host(
                                    host_registration.get("project_id", ""),
                                    host_registration.get("host_id", ""),
                                    host_registration.get("source_node_id", ""),
                                    host_registration.get("session_id", ""),
                                )
                                await ws.send_json({
                                    "type": "host_heartbeat",
                                    "ok": host_result.get("ok", False),
                                    "project_id": host_registration.get("project_id", ""),
                                    "host_id": host_registration.get("host_id", ""),
                                })
                            else:
                                await ws.send_json({"type": "host_heartbeat", "ok": False, "code": "no_canvas_host"})
                        else:
                            await ws.send_json({"type": "ack", "received": message_type})
                    except json.JSONDecodeError:
                        await ws.send_json({"type": "error", "message": "Invalid JSON"})
                elif msg.type == web.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", ws.exception())
        finally:
            await clear_host_registration()
            if subscribed_project_id:
                await unsubscribe(subscribed_project_id, ws)
        return ws
