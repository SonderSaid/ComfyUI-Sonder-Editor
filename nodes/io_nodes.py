import json
import os
import logging
import hashlib
import math
import random
import re
import shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime

import cv2
import numpy as np
import torch
import folder_paths

from PIL import Image

from ..server.timeline_state import ClipReference, Asset, LaneConfig, AudioTrack, classify_asset_path
from ..server import external_links
from ..server.atomic_io import atomic_replace
from ..server.project_manager import load_project, save_project
from ..server.path_security import (
    PathSecurityError,
    log_path_quarantine,
    normalize_project_relative_path,
    path_within,
    project_bridge_path,
    project_bridge_root,
    project_cache_path,
    project_media_path,
    project_media_root,
    resolve_under_root,
    safe_route_token,
)
from ..server.project_commit import (
    _copy_generated_asset_registration,
    _same_path_placeholder_can_upgrade,
    created_ids_since,
    save_generated_project,
    snapshot_item_ids,
)
from .metadata_collector import TRACKED_METADATA_CONTEXT_KEY, collector_chain_for_consumer
from ..server.color_correction import (
    COLOR_DRIFT_METADATA_KEY,
    fit_drift_correction,
    transform_tensor_to_uint8_frames,
)
from ..server.guide_collision import check_frame_count_excess
from ..server.session_registry import record_diag_event
from ..server.media_helpers import (
    CUSTOM_AUDIO_CODEC_OPTIONS,
    CUSTOM_CONTAINER_OPTIONS,
    CUSTOM_ENCODER_PRESET_OPTIONS,
    CUSTOM_OUTPUT_KIND_OPTIONS,
    CUSTOM_OUTPUT_KIND_PNG_SEQUENCE,
    CUSTOM_OUTPUT_KIND_VIDEO,
    CUSTOM_PIX_FMT_OPTIONS,
    CUSTOM_SAVE_VIDEO_PRESET,
    CUSTOM_VIDEO_CODEC_OPTIONS,
    CROP_POSITIONS,
    DEFAULT_CROP_POSITION,
    DEFAULT_FIT_MODE,
    DEFAULT_SAVE_VIDEO_PRESET,
    FIT_MODES,
    MediaProbeError,
    SAVE_VIDEO_PRESET_ORDER,
    SAVE_VIDEO_PRESETS,
    encode_video,
    extract_embedded_workflow_metadata,
    get_ffmpeg_path,
    metadata_for_save_preset,
    normalize_save_preset,
    output_extension_for_custom_options,
    output_extension_for_preset,
    probe_media_metadata,
    resolve_custom_export_options,
    save_video_encode_timeout_seconds,
    tensor_mode_for_preset,
    tensor_to_uint8_frames,
    write_audio_wav,
    write_png,
)

logger = logging.getLogger("sonder_editor")

BRIDGE_SCAN_SIDECAR = "_scan.json"
SAVE_PRESET_TOOLTIP = "Preset choices: " + " | ".join(
    f"{preset}: {SAVE_VIDEO_PRESETS[preset].get('description', '')}"
    for preset in SAVE_VIDEO_PRESET_ORDER
)
BRIDGE_STALE_DIR_TTL_SEC = 24 * 60 * 60
BRIDGE_PREVIEW_GRACE_SEC = 60.0
BRIDGE_POLL_INTERVAL_SEC = 0.25
BRIDGE_IDLE_SETTLE_SEC = 1.0
BRIDGE_FALLBACK_MIN_WAIT_SEC = 5.0
BRIDGE_MAX_WAIT_SEC = 60 * 60

_BRIDGE_REGISTRY_LOCK = threading.Lock()
_BRIDGE_REGISTRY = {}
_BRIDGE_PROMPT_WATCHERS = {}
_BRIDGE_PREVIEW_CLEANUP_TIMERS = {}
_BRIDGE_PROMPT_KEY_BY_OBJECT_ID = {}
_BRIDGE_PROMPT_OBJECT_IDS_BY_KEY = {}
_BRIDGE_PROMPT_QUEUE_HOOK_LOCK = threading.Lock()
_BRIDGE_HOOKED_PROMPT_QUEUE_ID = None


def _normalize_asset_folder(folder: str) -> str:
    return str(folder or "").strip().replace("\\", "/").strip("/")


def _ensure_asset_folder_metadata(project, folder: str) -> None:
    normalized = _normalize_asset_folder(folder)
    if not normalized:
        return
    existing = {
        _normalize_asset_folder(entry)
        for entry in project.metadata.get("asset_folders", [])
        if _normalize_asset_folder(entry)
    }
    if normalized in existing:
        return
    existing.add(normalized)
    project.metadata["asset_folders"] = sorted(existing)


def _copy_execution_context(project) -> dict:
    context = getattr(project, "_execution_context", None) or {}
    return dict(context) if isinstance(context, dict) else {}


def _save_generated_project(project, base_modified_at: str = "", created_ids=None):
    if base_modified_at:
        return save_generated_project(project, str(base_modified_at), created_ids)
    save_project(project)
    return project


EDITOR_EXPORT_SCHEMA_VERSION = "1.0"


def _json_clone(value):
    try:
        return json.loads(json.dumps(value, default=str))
    except Exception:
        return value


def _json_dumps_compact(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _public_execution_context(context: dict) -> dict:
    return {
        str(key): _json_clone(value)
        for key, value in dict(context or {}).items()
        if not str(key).startswith("_")
    }


def _produced_by_metadata() -> dict:
    return {
        "tool": "sonder-editor",
        "version": "",
        "comfyui_version": "",
    }


def _tracked_metadata_from_context(context: dict, prompt=None, consumer_unique_id=None) -> list:
    chain = collector_chain_for_consumer(context, prompt, consumer_unique_id)
    return [_json_clone(item) for item in chain if isinstance(item, dict)]


def _workflow_from_extra_pnginfo(extra_pnginfo):
    if isinstance(extra_pnginfo, dict) and isinstance(extra_pnginfo.get("workflow"), dict):
        return extra_pnginfo.get("workflow")
    return None


def _workflow_digest(workflow) -> str:
    if workflow is None:
        return ""
    try:
        payload = json.dumps(workflow, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        payload = str(workflow)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compact_editor_export(project, context: dict, *, has_embedded_workflow=False, workflow=None, prompt=None, consumer_unique_id=None) -> dict:
    export = {
        "schema_version": EDITOR_EXPORT_SCHEMA_VERSION,
        # Additive fields stay at schema 1.0; bump only for shape-breaking changes.
        "produced_by": _produced_by_metadata(),
        "exported_at": datetime.now().isoformat(),
        "project_name": getattr(project, "name", ""),
        "scene_id": str(context.get("scene_id") or ""),
        "scene_name": str(context.get("scene_name") or ""),
        "tracked_metadata": _tracked_metadata_from_context(context, prompt, consumer_unique_id),
        "has_embedded_workflow": bool(has_embedded_workflow),
    }
    digest = _workflow_digest(workflow) if has_embedded_workflow else ""
    if digest:
        export["workflow_sha256"] = digest
    manifest = context.get("guide_injection") if isinstance(context, dict) else None
    if isinstance(manifest, dict) and (
        int(manifest.get("collision_count", 0) or 0) > 0
        or int(manifest.get("driver_driver_collision_count", 0) or 0) > 0
    ):
        detected = [
            {
                "guide_id": str(entry.get("guide_id") or ""),
                "from": int(entry.get("original_local_idx", 0) or 0),
                "to": int(entry.get("effective_local_idx", 0) or 0),
                "collided_with": str(entry.get("collided_with") or ""),
            }
            for entry in (manifest.get("entries") or [])
            if isinstance(entry, dict) and entry.get("collided") is True
        ]
        enabled = manifest.get("auto_offset_enabled") is True
        export["guide_collision"] = {
            "enabled": enabled,
            "applied": detected if enabled else [],
            "detected": detected,
            "driver_driver_collision_count": int(manifest.get("driver_driver_collision_count", 0) or 0),
            "predicted_unresolved": manifest.get("predicted_unresolved") is True,
        }
    return export


def _guide_bleed_check_from_context(context: dict, project_id: str = "") -> dict:
    manifest = context.get("guide_injection") if isinstance(context, dict) else None
    if not isinstance(manifest, dict):
        return {"armed": False}
    constraint = manifest.get("frame_constraint")
    constraint = constraint if isinstance(constraint, dict) else {}
    return {
        "armed": manifest.get("predicted_unresolved") is True,
        "expected_frame_count": int(manifest.get("frame_count", context.get("frame_count", 0)) or 0),
        "step": int(constraint.get("step", 1) or 1),
        "max_excess_latents": int(manifest.get("max_excess_latents", 0) or 0),
        "guide_ids": [
            str(entry.get("guide_id") or "") for entry in (manifest.get("entries") or [])
            if isinstance(entry, dict) and entry.get("collided") is True
        ],
        "project_id": str(project_id or ""),
    }


def _record_guide_bleed_if_needed(check: dict, actual_frame_count: int, path: str) -> bool:
    if not isinstance(check, dict) or check.get("armed") is not True:
        return False
    expected = int(check.get("expected_frame_count", 0) or 0)
    actual = int(actual_frame_count or 0)
    if not check_frame_count_excess(
        expected, actual, int(check.get("step", 1) or 1),
        int(check.get("max_excess_latents", 0) or 0),
    ):
        return False
    details = {
        "path": str(path or ""),
        "expected_frame_count": expected,
        "actual_frame_count": actual,
        "excess_frames": actual - expected,
        "guide_ids": list(check.get("guide_ids") or []),
    }
    logger.warning("guide bleed suspected: %s", details)
    record_diag_event(
        "guide_bleed_suspected",
        project_id=str(check.get("project_id") or ""),
        **details,
    )
    return True


def _bridge_generation_params(project, context: dict, prompt=None, consumer_unique_id=None) -> dict:
    return {
        "scene_id": str((context or {}).get("scene_id") or ""),
        "scene_name": str((context or {}).get("scene_name") or ""),
        "editor_export": _compact_editor_export(
            project,
            context,
            has_embedded_workflow=False,
            workflow=None,
            prompt=prompt,
            consumer_unique_id=consumer_unique_id,
        ),
    }


def _generation_params_with_detected_workflow(generation_params: dict, file_path: str) -> dict:
    params = dict(generation_params or {})
    editor_export = params.get("editor_export")
    if not isinstance(editor_export, dict):
        return params
    workflow = extract_embedded_workflow_metadata(file_path)
    if not isinstance(workflow, dict):
        return params
    updated_export = dict(editor_export)
    updated_export["has_embedded_workflow"] = True
    digest = _workflow_digest(workflow)
    if digest:
        updated_export["workflow_sha256"] = digest
    params["editor_export"] = updated_export
    return params


def _file_metadata_payload(prompt, workflow, editor_export: dict) -> dict[str, str]:
    file_editor_export = dict(editor_export)
    if prompt is not None:
        file_editor_export["prompt"] = _json_clone(prompt)
    if workflow is not None:
        file_editor_export["workflow"] = _json_clone(workflow)
    payload = {"editor_export": _json_dumps_compact(file_editor_export)}
    if prompt is not None:
        payload["prompt"] = _json_dumps_compact(prompt)
    if workflow is not None:
        payload["workflow"] = _json_dumps_compact(workflow)
    return payload


def _find_queue_job(project, queue_job_id: str):
    if not project or not queue_job_id:
        return None
    for job in getattr(project, "generation_queue", []) or []:
        if getattr(job, "job_id", "") == queue_job_id:
            return job
    return None


def _mark_later_batch_jobs_failed(project, queue_job):
    batch_id = str(getattr(queue_job, "batch_id", "") or "")
    if not project or not batch_id:
        return

    queue = getattr(project, "generation_queue", []) or []
    failed_queue_index = None
    failed_job_id = str(getattr(queue_job, "job_id", "") or "")
    failed_batch_index = int(getattr(queue_job, "batch_index", 0) or 0)

    for idx, job in enumerate(queue):
        if job is queue_job or (failed_job_id and getattr(job, "job_id", "") == failed_job_id):
            failed_queue_index = idx
            break

    skip_error = f"Skipped after earlier batch failure ({failed_job_id or 'unknown job'})"
    for idx, job in enumerate(queue):
        if job is queue_job:
            continue
        if str(getattr(job, "batch_id", "") or "") != batch_id:
            continue
        if (getattr(job, "status", "pending") or "pending").lower() != "pending":
            continue

        job_batch_index = int(getattr(job, "batch_index", 0) or 0)
        is_later_chunk = job_batch_index > failed_batch_index
        if failed_queue_index is not None and idx > failed_queue_index:
            is_later_chunk = True
        if not is_later_chunk:
            continue

        job.status = "failed"
        job.error = skip_error
        job.progress = 0.0


def _mark_queue_job_failed(project, queue_job_id: str, error_message: str) -> bool:
    queue_job = _find_queue_job(project, queue_job_id)
    if not queue_job:
        return False
    if (queue_job.status or "pending").lower() != "running":
        logger.info(
            "Queue fail skipped for %s because status=%s",
            queue_job_id,
            getattr(queue_job, "status", ""),
        )
        return False
    queue_job.status = "failed"
    queue_job.error = str(error_message)
    queue_job.progress = 0.0
    queue_job.completed_at = ""
    queue_job.result_asset_id = ""
    _mark_later_batch_jobs_failed(project, queue_job)
    return True


def _mark_queue_job_completed(project, queue_job_id: str, result_asset_id: str) -> bool:
    queue_job = _find_queue_job(project, queue_job_id)
    if not queue_job:
        return False
    if (queue_job.status or "pending").lower() != "running":
        logger.info(
            "Queue completion skipped for %s because status=%s",
            queue_job_id,
            getattr(queue_job, "status", ""),
        )
        return False
    queue_job.status = "completed"
    queue_job.progress = 1.0
    queue_job.error = ""
    queue_job.completed_at = datetime.now().isoformat()
    queue_job.result_asset_id = result_asset_id
    return True


def _require_project_media_root(project) -> str:
    media_dir = project_media_root(project)
    if not media_dir:
        raise ValueError("Invalid project media directory")
    os.makedirs(media_dir, exist_ok=True)
    return media_dir


def _project_media_file(project, filename: str, *, purpose: str) -> tuple[str, str]:
    rel_path = os.path.join("media", filename)
    abs_path = project_media_path(project, filename, purpose=purpose)
    if not abs_path:
        raise ValueError(f"Invalid {purpose}")
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    return rel_path, abs_path


def _project_thumbnail_path(project, asset_id: str, *, purpose: str = "generated thumbnail path") -> str:
    try:
        safe_id = safe_route_token(asset_id, "asset id")
    except PathSecurityError as exc:
        log_path_quarantine(
            purpose=purpose,
            path=str(asset_id or ""),
            root=getattr(project, "project_dir", "") or "",
            reason=str(exc),
        )
        return ""
    thumb_path = project_cache_path(
        project,
        os.path.join("thumbnails", f"{safe_id}.png"),
        purpose=purpose,
    )
    if thumb_path:
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
    return thumb_path

_BRIDGE_SANITIZE_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


def _sanitize_bridge_component(value: str) -> str:
    replaced = _BRIDGE_SANITIZE_PATTERN.sub("_", str(value or ""))
    while "__" in replaced:
        replaced = replaced.replace("__", "_")
    return replaced.strip("_")


def _safe_output_stem(value: str, fallback: str = "output") -> str:
    return (_sanitize_bridge_component(value) or fallback)[:120]


def _safe_bridge_dir_name(prompt_key: str, bridge_node_id: str) -> str:
    prompt_part = _safe_output_stem(prompt_key, "prompt")
    node_part = _safe_output_stem(bridge_node_id, "bridge")
    try:
        return safe_route_token(f"{prompt_part}_{node_part}", "bridge directory")
    except PathSecurityError:
        return f"prompt_{uuid.uuid4().hex[:8]}_bridge"


def _safe_extension_from_filename(filename: str) -> str:
    _stem, ext = os.path.splitext(os.path.basename(str(filename or "")))
    if not ext:
        return ""
    ext = re.sub(r"[^A-Za-z0-9.]+", "_", ext)
    if ext in {"", "."}:
        return ""
    return ext[:32]


def _build_bridge_naming_stem(project, context: dict, prefix: str) -> str:
    parts: list[str] = []
    prefix_part = _sanitize_bridge_component(prefix)
    if prefix_part:
        parts.append(prefix_part)
    project_part = _sanitize_bridge_component(getattr(project, "name", "") or "")
    parts.append(project_part or "bridge")
    scene_name = context.get("scene_name", "") if isinstance(context, dict) else ""
    scene_part = _sanitize_bridge_component(scene_name)
    if scene_part:
        parts.append(scene_part)
    parts.append(datetime.now().strftime("%Y%m%d-%H%M%S"))
    return "_".join(parts)


def _safe_bridge_relpath(path: str, root: str) -> str:
    rel_path = os.path.relpath(os.path.realpath(path), os.path.realpath(root)).replace("\\", "/")
    return normalize_project_relative_path(rel_path)


def _strict_realpath_within(parent: str, child: str) -> bool:
    """Contain bridge scans by real paths even when external-link trust is enabled.

    Bridge output is machine-managed. A planted directory junction there must never be
    walked into or moved from, so this deliberately does not use path_security.path_within.
    """
    try:
        parent_real = os.path.normcase(os.path.realpath(str(parent or "")))
        child_real = os.path.normcase(os.path.realpath(str(child or "")))
        return os.path.commonpath([parent_real, child_real]) == parent_real
    except (OSError, ValueError):
        return False


def _write_bridge_sidecar(
    bridge_dir: str,
    existed: list[str],
    *,
    preview_expires_at: float | None = None,
) -> None:
    sidecar_path = resolve_under_root(
        bridge_dir,
        BRIDGE_SCAN_SIDECAR,
        purpose="bridge scan sidecar",
    )
    if not sidecar_path:
        raise ValueError("Invalid bridge sidecar path")

    payload = {"existed": sorted(set(existed))}
    if preview_expires_at is not None:
        expires_at = float(preview_expires_at)
        if not math.isfinite(expires_at) or expires_at <= 0:
            raise ValueError("Invalid bridge preview expiry")
        payload["preview_expires_at"] = expires_at

    temp_name = f".{BRIDGE_SCAN_SIDECAR}.{uuid.uuid4().hex}.tmp"
    temp_path = resolve_under_root(
        bridge_dir,
        temp_name,
        purpose="bridge scan sidecar temp",
    )
    if not temp_path:
        raise ValueError("Invalid bridge sidecar temp path")
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(temp_path, sidecar_path)
    except Exception:
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass
        raise


def _read_bridge_sidecar_payload(bridge_dir: str) -> dict:
    sidecar_path = resolve_under_root(
        bridge_dir,
        BRIDGE_SCAN_SIDECAR,
        purpose="bridge scan sidecar",
        must_exist=True,
        log=False,
    )
    if not os.path.isfile(sidecar_path):
        return {}
    try:
        with open(sidecar_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_bridge_sidecar(bridge_dir: str) -> set[str]:
    existed = _read_bridge_sidecar_payload(bridge_dir).get("existed", [])
    return {
        str(entry).replace("\\", "/").strip("/")
        for entry in existed
        if str(entry or "").strip()
    }


def _bridge_preview_expires_at(bridge_dir: str) -> float | None:
    raw_value = _read_bridge_sidecar_payload(bridge_dir).get("preview_expires_at")
    try:
        expires_at = float(raw_value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(expires_at) or expires_at <= 0:
        return None
    return expires_at


def _list_bridge_files(bridge_dir: str) -> list[str]:
    results = []
    bridge_real = os.path.realpath(str(bridge_dir or ""))
    if not os.path.isdir(bridge_real):
        return results
    for root, dirs, files in os.walk(bridge_real):
        kept_dirs = []
        for dirname in dirs:
            dir_path = os.path.join(root, dirname)
            try:
                if os.path.islink(dir_path) or external_links.is_reparse_child(root, dirname):
                    log_path_quarantine(
                        purpose="bridge output scan",
                        path=dir_path,
                        root=bridge_real,
                        reason="symlink directory skipped",
                    )
                    continue
                if not _strict_realpath_within(bridge_real, dir_path):
                    log_path_quarantine(
                        purpose="bridge output scan",
                        path=dir_path,
                        root=bridge_real,
                        reason="directory escapes bridge root",
                    )
                    continue
            except OSError:
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs
        for filename in files:
            full_path = os.path.join(root, filename)
            try:
                if os.path.islink(full_path) or not os.path.isfile(full_path):
                    log_path_quarantine(
                        purpose="bridge output scan",
                        path=full_path,
                        root=bridge_real,
                        reason="non-regular file skipped",
                    )
                    continue
                if not _strict_realpath_within(bridge_real, full_path):
                    log_path_quarantine(
                        purpose="bridge output scan",
                        path=full_path,
                        root=bridge_real,
                        reason="file escapes bridge root",
                    )
                    continue
                rel_path = _safe_bridge_relpath(full_path, bridge_real).strip("/")
            except (OSError, PathSecurityError) as exc:
                log_path_quarantine(
                    purpose="bridge output scan",
                    path=full_path,
                    root=bridge_real,
                    reason=str(exc),
                )
                continue
            if rel_path == BRIDGE_SCAN_SIDECAR or (
                rel_path.startswith(f".{BRIDGE_SCAN_SIDECAR}.") and rel_path.endswith(".tmp")
            ):
                continue
            results.append(rel_path)
    return sorted(results)


def _latest_bridge_change_time(bridge_dir: str) -> float:
    bridge_real = os.path.realpath(str(bridge_dir or ""))
    latest = os.path.getmtime(bridge_real) if os.path.isdir(bridge_real) else 0.0
    for root, dirs, files in os.walk(bridge_real):
        kept_dirs = []
        for dirname in dirs:
            dir_path = os.path.join(root, dirname)
            try:
                if os.path.islink(dir_path) or external_links.is_reparse_child(root, dirname):
                    continue
                if not _strict_realpath_within(bridge_real, dir_path):
                    continue
                latest = max(latest, os.path.getmtime(dir_path))
            except OSError:
                continue
            kept_dirs.append(dirname)
        dirs[:] = kept_dirs
        for filename in files:
            path = os.path.join(root, filename)
            try:
                if os.path.islink(path) or not _strict_realpath_within(bridge_real, path):
                    continue
                latest = max(latest, os.path.getmtime(path))
            except OSError:
                continue
    return latest


def _cleanup_stale_bridge_dirs(project_dir: str) -> None:
    bridge_root = project_bridge_root(project_dir, must_exist=True)
    if not os.path.isdir(bridge_root):
        return
    now = time.time()
    cutoff = now - BRIDGE_STALE_DIR_TTL_SEC
    for entry in os.scandir(bridge_root):
        if not entry.is_dir(follow_symlinks=False):
            continue
        if external_links.is_reparse_child(bridge_root, entry.name):
            log_path_quarantine(
                purpose="stale bridge cleanup",
                path=entry.path,
                root=bridge_root,
                reason="reparse directory skipped",
            )
            continue
        target = os.path.abspath(entry.path) if external_links.is_enabled() else os.path.realpath(entry.path)
        if not path_within(bridge_root, target):
            log_path_quarantine(
                purpose="stale bridge cleanup",
                path=entry.path,
                root=bridge_root,
                reason="entry escapes bridge root",
            )
            continue
        preview_expires_at = _bridge_preview_expires_at(target)
        if preview_expires_at is not None:
            if preview_expires_at <= now:
                _cleanup_bridge_output_dir(project_dir, target)
            else:
                _schedule_bridge_preview_cleanup(project_dir, target, preview_expires_at)
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        recovered_stem = f"bridge_recovered_{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        orphan_entry = {
            "project_dir": project_dir,
            "prompt_key": f"recovered_{uuid.uuid4().hex[:8]}",
            "bridge_node_id": "recovered",
            "bridge_dir": target,
            "prompt_key_source": "recovered",
            "target_folder": "",
            "mark_queue_complete": False,
            "queue_job_id": "",
            "prompt_text": "",
            "generation_params": {},
            "naming_stem": recovered_stem,
            "base_modified_at": "",
        }
        try:
            _finalize_bridge_entry(orphan_entry)
        except Exception:
            logger.exception("Failed to adopt stale bridge dir %s", target)
            if path_within(bridge_root, target):
                shutil.rmtree(target, ignore_errors=True)


def _build_bridge_output_paths(project, prompt_key: str, bridge_node_id: str, naming_stem: str) -> tuple[str, str]:
    bridge_name = _safe_bridge_dir_name(prompt_key, bridge_node_id)
    bridge_dir = project_bridge_path(project, bridge_name, purpose="bridge output directory")
    if not bridge_dir:
        raise ValueError("Invalid bridge output directory")
    import folder_paths as current_folder_paths

    bridge_path = os.path.abspath(bridge_dir) if external_links.is_enabled() else os.path.realpath(bridge_dir)
    stem = _safe_output_stem(naming_stem, "out")
    filename_prefix = ""
    output_root_raw = str(current_folder_paths.get_output_directory() or "")
    if output_root_raw:
        output_root = os.path.realpath(output_root_raw)
        if path_within(output_root, bridge_path):
            relative_dir = os.path.relpath(bridge_path, output_root).replace("\\", "/")
            try:
                relative_dir = normalize_project_relative_path(relative_dir)
            except PathSecurityError as exc:
                raise ValueError(f"Invalid bridge output prefix: {exc}") from exc
            filename_prefix = f"{relative_dir}/{stem}"
        else:
            logger.warning(
                "Bridge output directory %s is outside ComfyUI output root %s; native filename_prefix disabled",
                bridge_path,
                output_root,
            )
    return bridge_dir, filename_prefix


def _prepare_bridge_output_dir(project, prompt_key: str, bridge_node_id: str, naming_stem: str) -> tuple[str, str]:
    _cleanup_stale_bridge_dirs(project.project_dir)
    bridge_dir, filename_prefix = _build_bridge_output_paths(project, prompt_key, bridge_node_id, naming_stem)
    os.makedirs(bridge_dir, exist_ok=True)
    existed = _list_bridge_files(bridge_dir)
    _write_bridge_sidecar(bridge_dir, existed)
    return bridge_dir, filename_prefix


def _bridge_unique_media_name(media_dir: str, basename: str) -> str:
    stem, ext = os.path.splitext(basename)
    candidate = basename
    suffix = 1
    while os.path.lexists(os.path.join(media_dir, candidate)):
        candidate = f"{stem}_{suffix}{ext}"
        suffix += 1
    return candidate


def _unique_media_subfolder(media_dir: str, folder_name: str) -> str:
    stem = _sanitize_bridge_component(folder_name) or "output"
    candidate = stem
    suffix = 1
    while os.path.lexists(os.path.join(media_dir, candidate)):
        candidate = f"{stem}_{suffix}"
        suffix += 1
    return candidate


def _save_image_asset_thumbnail(project, asset: Asset, filepath: str) -> None:
    try:
        from ..server.thumbnail_service import ensure_thumbnail

        thumb_path = _project_thumbnail_path(project, asset.asset_id, purpose="image asset thumbnail path")
        if thumb_path:
            ensure_thumbnail("image", filepath, thumb_path)
    except Exception as exc:
        logger.warning("Failed to generate image thumbnail for %s: %s", filepath, exc)


def _save_custom_png_sequence(
    project,
    rgb_frames: np.ndarray,
    filename_prefix: str,
    metadata: dict,
    file_metadata: dict[str, str] | None = None,
) -> tuple[str, list[Asset]]:
    media_dir = _require_project_media_root(project)
    stem = _safe_output_stem(filename_prefix, "output")
    frame_count, h, w = rgb_frames.shape[:3]
    compression = int(metadata.get("custom_png_compression", 0) or 0)
    assets: list[Asset] = []

    if frame_count == 1:
        filename = _bridge_unique_media_name(media_dir, f"{stem}.png")
        rel_path, filepath = _project_media_file(project, filename, purpose="custom PNG output path")
        write_png(filepath, rgb_frames[0], compression=compression, metadata=file_metadata)
        asset = Asset(
            name=filename,
            asset_type="image",
            path=rel_path,
            width=w,
            height=h,
            frame_count=1,
            generation_params={
                **metadata,
                "image_sequence": False,
                "sequence_total": 1,
                "sequence_index": 1,
            },
        )
        project.add_asset(asset)
        _save_image_asset_thumbnail(project, asset, filepath)
        return filepath, [asset]

    folder_name = _unique_media_subfolder(media_dir, stem)
    folder_path = project_media_path(project, folder_name, purpose="custom PNG sequence folder")
    if not folder_path:
        raise ValueError("Invalid custom PNG sequence folder")
    os.makedirs(folder_path, exist_ok=True)
    _ensure_asset_folder_metadata(project, folder_name)

    for idx, frame in enumerate(rgb_frames, start=1):
        filename = f"{stem}_{idx:04d}.png"
        rel_path = os.path.join("media", folder_name, filename)
        filepath = project_media_path(project, os.path.join(folder_name, filename), purpose="custom PNG sequence frame")
        if not filepath:
            raise ValueError("Invalid custom PNG sequence frame path")
        write_png(filepath, frame, compression=compression, metadata=file_metadata if idx == 1 else None)
        frame_metadata = dict(metadata)
        editor_export = frame_metadata.get("editor_export")
        if idx != 1 and isinstance(editor_export, dict):
            editor_export = dict(editor_export)
            editor_export["has_embedded_workflow"] = False
            editor_export.pop("workflow_sha256", None)
            frame_metadata["editor_export"] = editor_export
        asset = Asset(
            name=filename,
            asset_type="image",
            path=rel_path,
            width=w,
            height=h,
            frame_count=1,
            folder=folder_name,
            generation_params={
                **frame_metadata,
                "image_sequence": True,
                "sequence_folder": folder_name,
                "sequence_total": frame_count,
                "sequence_index": idx,
            },
        )
        project.add_asset(asset)
        _save_image_asset_thumbnail(project, asset, filepath)
        assets.append(asset)
    return folder_path, assets


def _extract_bridge_asset_metadata(source_path: str, asset_type: str) -> dict:
    return probe_media_metadata(
        source_path,
        asset_type,
        strict=asset_type in {"video", "image", "audio"},
    )


def _load_prompt_server_instance():
    try:
        import importlib
        server_mod = importlib.import_module("server")
        prompt_server = getattr(server_mod, "PromptServer", None)
        return getattr(prompt_server, "instance", None) if prompt_server else None
    except Exception:
        return None


def _set_bridge_prompt_key_for_object(prompt_key: str, prompt) -> None:
    normalized_key = str(prompt_key or "").strip()
    if not normalized_key or not isinstance(prompt, dict):
        return
    object_id = id(prompt)
    with _BRIDGE_REGISTRY_LOCK:
        previous_key = _BRIDGE_PROMPT_KEY_BY_OBJECT_ID.get(object_id)
        if previous_key and previous_key != normalized_key:
            object_ids = _BRIDGE_PROMPT_OBJECT_IDS_BY_KEY.get(previous_key)
            if isinstance(object_ids, set):
                object_ids.discard(object_id)
                if not object_ids:
                    _BRIDGE_PROMPT_OBJECT_IDS_BY_KEY.pop(previous_key, None)
        _BRIDGE_PROMPT_KEY_BY_OBJECT_ID[object_id] = normalized_key
        _BRIDGE_PROMPT_OBJECT_IDS_BY_KEY.setdefault(normalized_key, set()).add(object_id)


def _looks_like_prompt_graph(value) -> bool:
    if not isinstance(value, dict):
        return False
    if any(key in value for key in ("_prompt_id", "prompt_id", "__prompt_id__", "__prompt_key__")):
        return True
    return any(
        isinstance(entry, dict) and ("class_type" in entry or "inputs" in entry)
        for entry in value.values()
    )


def _find_prompt_graph_in_structure(structure, seen=None):
    if _looks_like_prompt_graph(structure):
        return structure
    if seen is None:
        seen = set()
    marker = id(structure)
    if marker in seen:
        return None
    seen.add(marker)
    if isinstance(structure, dict):
        for value in structure.values():
            found = _find_prompt_graph_in_structure(value, seen)
            if found is not None:
                return found
    elif isinstance(structure, (list, tuple, set)):
        for value in structure:
            found = _find_prompt_graph_in_structure(value, seen)
            if found is not None:
                return found
    return None


def _extract_prompt_key_from_queue_get_result(result) -> tuple[str, dict | None]:
    prompt = _find_prompt_graph_in_structure(result)
    if isinstance(result, tuple):
        if len(result) >= 2 and isinstance(result[1], (str, int)) and str(result[1]).strip():
            return str(result[1]).strip(), prompt
        payload = result[0]
        if isinstance(payload, (tuple, list)) and len(payload) >= 2 and isinstance(payload[1], (str, int)) and str(payload[1]).strip():
            return str(payload[1]).strip(), prompt
    explicit = _extract_prompt_key_from_prompt(prompt)
    if explicit:
        return explicit, prompt
    return "", prompt


def _extract_prompt_key_from_task_done_call(args, kwargs) -> str:
    if isinstance(kwargs, dict):
        for key in ("item_id", "prompt_id", "task_id", "id"):
            value = kwargs.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
    if args:
        value = args[0]
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _install_bridge_prompt_queue_hooks() -> bool:
    global _BRIDGE_HOOKED_PROMPT_QUEUE_ID

    prompt_server = _load_prompt_server_instance()
    prompt_queue = getattr(prompt_server, "prompt_queue", None) if prompt_server else None
    if prompt_queue is None:
        return False

    with _BRIDGE_PROMPT_QUEUE_HOOK_LOCK:
        current_queue_id = id(prompt_queue)
        if _BRIDGE_HOOKED_PROMPT_QUEUE_ID == current_queue_id:
            return (
                callable(getattr(prompt_queue, "get", None))
                and callable(getattr(prompt_queue, "task_done", None))
            )

        original_get = getattr(prompt_queue, "get", None)
        if callable(original_get) and not getattr(original_get, "_sonder_bridge_wrapped", False):
            def wrapped_get(*args, **kwargs):
                result = original_get(*args, **kwargs)
                try:
                    prompt_key, prompt = _extract_prompt_key_from_queue_get_result(result)
                    _set_bridge_prompt_key_for_object(prompt_key, prompt)
                except Exception:
                    logger.exception("Failed to capture bridge prompt identity from prompt queue get()")
                return result

            wrapped_get._sonder_bridge_wrapped = True
            setattr(prompt_queue, "get", wrapped_get)

        original_task_done = getattr(prompt_queue, "task_done", None)
        if callable(original_task_done) and not getattr(original_task_done, "_sonder_bridge_wrapped", False):
            def wrapped_task_done(*args, **kwargs):
                prompt_key = _extract_prompt_key_from_task_done_call(args, kwargs)
                try:
                    return original_task_done(*args, **kwargs)
                finally:
                    if prompt_key:
                        try:
                            _finalize_prompt_bridges(prompt_key, source="task_done")
                        except Exception:
                            logger.exception("Bridge finalization failed from prompt queue task_done for %s", prompt_key)

            wrapped_task_done._sonder_bridge_wrapped = True
            setattr(prompt_queue, "task_done", wrapped_task_done)

        _BRIDGE_HOOKED_PROMPT_QUEUE_ID = current_queue_id
        return (
            callable(getattr(prompt_queue, "get", None))
            and callable(getattr(prompt_queue, "task_done", None))
        )


def _structure_contains_prompt(structure, prompt, seen=None) -> bool:
    if structure is prompt:
        return True
    if seen is None:
        seen = set()
    marker = id(structure)
    if marker in seen:
        return False
    seen.add(marker)
    if isinstance(structure, dict):
        for key, value in structure.items():
            if key == prompt or value is prompt:
                return True
            if _structure_contains_prompt(value, prompt, seen):
                return True
    elif isinstance(structure, (list, tuple, set)):
        for value in structure:
            if value is prompt or _structure_contains_prompt(value, prompt, seen):
                return True
    return False


def _prompt_currently_running(prompt_key: str, prompt) -> bool:
    prompt_server = _load_prompt_server_instance()
    prompt_queue = getattr(prompt_server, "prompt_queue", None) if prompt_server else None
    if prompt_queue is None:
        return False

    currently_running = getattr(prompt_queue, "currently_running", None)
    if isinstance(currently_running, dict):
        if str(prompt_key) in {str(key) for key in currently_running.keys()}:
            return True
        if prompt is not None and _structure_contains_prompt(currently_running, prompt):
            return True
    elif currently_running:
        if prompt is not None and _structure_contains_prompt(currently_running, prompt):
            return True
        if str(prompt_key) in {str(value) for value in currently_running}:
            return True
    return False


def _extract_prompt_key_from_prompt(prompt) -> str:
    if not isinstance(prompt, dict):
        return ""
    for key in ("_prompt_id", "prompt_id", "__prompt_id__", "__prompt_key__"):
        value = prompt.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _resolve_bridge_prompt_key(prompt) -> tuple[str, str]:
    hook_ready = _install_bridge_prompt_queue_hooks()
    object_id = id(prompt) if isinstance(prompt, dict) else None
    with _BRIDGE_REGISTRY_LOCK:
        if object_id is not None and object_id in _BRIDGE_PROMPT_KEY_BY_OBJECT_ID:
            return _BRIDGE_PROMPT_KEY_BY_OBJECT_ID[object_id], ("queue_hook" if hook_ready else "queue_map")

    explicit = _extract_prompt_key_from_prompt(prompt)
    if explicit:
        _set_bridge_prompt_key_for_object(explicit, prompt)
        return explicit, "prompt_field"

    object_id = id(prompt) if isinstance(prompt, dict) else None
    with _BRIDGE_REGISTRY_LOCK:
        if object_id is not None and object_id in _BRIDGE_PROMPT_KEY_BY_OBJECT_ID:
            return _BRIDGE_PROMPT_KEY_BY_OBJECT_ID[object_id], ("queue_hook" if hook_ready else "queue_map")
        prompt_key = uuid.uuid4().hex[:12]
        if object_id is not None:
            _BRIDGE_PROMPT_KEY_BY_OBJECT_ID[object_id] = prompt_key
            _BRIDGE_PROMPT_OBJECT_IDS_BY_KEY.setdefault(prompt_key, set()).add(object_id)
        return prompt_key, "fallback"


def _cleanup_prompt_key_state(prompt_key: str) -> None:
    object_ids = _BRIDGE_PROMPT_OBJECT_IDS_BY_KEY.pop(prompt_key, set())
    for object_id in object_ids:
        if _BRIDGE_PROMPT_KEY_BY_OBJECT_ID.get(object_id) == prompt_key:
            _BRIDGE_PROMPT_KEY_BY_OBJECT_ID.pop(object_id, None)


def _validated_bridge_dir(project, bridge_dir: str, *, must_exist: bool = True) -> str:
    root = project_bridge_root(project, must_exist=must_exist)
    if not root:
        return ""
    target = os.path.abspath(str(bridge_dir or "")) if external_links.is_enabled() else os.path.realpath(str(bridge_dir or ""))
    if not target or not path_within(root, target):
        log_path_quarantine(
            purpose="bridge output directory",
            path=str(bridge_dir or ""),
            root=root,
            reason="entry escapes bridge root",
        )
        return ""
    if must_exist and not os.path.isdir(target):
        return ""
    return target


def _cleanup_bridge_output_dir(project, bridge_dir: str) -> bool:
    target = _validated_bridge_dir(project, bridge_dir, must_exist=True)
    if not target or not os.path.isdir(target):
        return True

    preview_expires_at = _bridge_preview_expires_at(target)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        logger.warning("Failed to clean bridge output directory %s: %s", target, exc)
        if os.path.isdir(target) and preview_expires_at is not None:
            try:
                _write_bridge_sidecar(
                    target,
                    _list_bridge_files(target),
                    preview_expires_at=preview_expires_at,
                )
            except Exception:
                logger.warning("Failed to restore bridge preview cleanup marker for %s", target, exc_info=True)
        return False
    return not os.path.exists(target)


def _schedule_bridge_preview_cleanup(project, bridge_dir: str, expires_at: float):
    project_dir = project if isinstance(project, str) else getattr(project, "project_dir", "")
    normalized_dir = os.path.abspath(bridge_dir) if external_links.is_enabled() else os.path.realpath(bridge_dir)
    timer_key = os.path.normcase(normalized_dir)
    expiry = float(expires_at)
    delay = max(0.0, expiry - time.time())

    def _cleanup_when_expired() -> None:
        reschedule = False
        try:
            if expiry - time.time() > 0:
                reschedule = True
            else:
                _cleanup_bridge_output_dir(project_dir, normalized_dir)
        finally:
            with _BRIDGE_REGISTRY_LOCK:
                current = _BRIDGE_PREVIEW_CLEANUP_TIMERS.get(timer_key)
                if current and current[0] is timer:
                    _BRIDGE_PREVIEW_CLEANUP_TIMERS.pop(timer_key, None)
        if reschedule:
            _schedule_bridge_preview_cleanup(project_dir, normalized_dir, expiry)

    with _BRIDGE_REGISTRY_LOCK:
        existing = _BRIDGE_PREVIEW_CLEANUP_TIMERS.get(timer_key)
        if existing:
            existing_timer, existing_expiry = existing
            try:
                existing_alive = existing_timer.is_alive()
            except AttributeError:
                existing_alive = True
            if existing_alive and abs(float(existing_expiry) - expiry) < 0.001:
                return existing_timer
            try:
                existing_timer.cancel()
            except AttributeError:
                pass
        timer = threading.Timer(delay, _cleanup_when_expired)
        timer.daemon = True
        _BRIDGE_PREVIEW_CLEANUP_TIMERS[timer_key] = (timer, expiry)
    timer.start()
    return timer


def _normalize_asset_path_for_compare(path: str) -> str:
    return str(path or "").replace("\\", "/").strip()


def _upgrade_bridge_placeholder_asset(project, source_asset: Asset) -> tuple[Asset, bool]:
    source_path = _normalize_asset_path_for_compare(source_asset.path)
    if not source_path:
        return source_asset, False
    for existing in getattr(project, "assets", []) or []:
        if _normalize_asset_path_for_compare(existing.path) != source_path:
            continue
        if not _same_path_placeholder_can_upgrade(existing):
            continue
        _copy_generated_asset_registration(existing, source_asset)
        return existing, True
    return source_asset, False


def _finalize_bridge_entry(entry: dict) -> list[Asset]:
    project = load_project(entry["project_dir"])
    _pre_item_ids = snapshot_item_ids(project)
    queue_job_id = str(entry.get("queue_job_id") or "")
    target_folder = _normalize_asset_folder(entry.get("target_folder", ""))
    bridge_dir = _validated_bridge_dir(project, entry.get("bridge_dir", ""), must_exist=True)
    if not bridge_dir:
        raise ValueError("Invalid bridge output directory")
    naming_stem = _safe_output_stem(entry.get("naming_stem") or "", "bridge_out")

    existed = _read_bridge_sidecar(bridge_dir)
    new_files = sorted(
        rel_path
        for rel_path in _list_bridge_files(bridge_dir)
        if rel_path not in existed
    )

    registered_assets = []
    media_dir = _require_project_media_root(project)

    counter = 0
    planned_final_names = set()
    pending_moves = []

    def _reserve_final_name(target_basename: str) -> str:
        final_name = _bridge_unique_media_name(media_dir, target_basename)
        if final_name not in planned_final_names:
            planned_final_names.add(final_name)
            return final_name
        stem, ext = os.path.splitext(target_basename)
        suffix = 1
        while True:
            candidate = f"{stem}_{suffix}{ext}"
            final_name = _bridge_unique_media_name(media_dir, candidate)
            if final_name not in planned_final_names:
                planned_final_names.add(final_name)
                return final_name
            suffix += 1

    def _publish_bridge_output_file(pending: dict) -> str:
        source_path = resolve_under_root(
            bridge_dir,
            pending["rel_path"],
            purpose="bridge output source file",
            must_exist=True,
        )
        final_path = pending["final_path"]
        if (
            not source_path
            or os.path.islink(source_path)
            or not os.path.isfile(source_path)
            or not path_within(bridge_dir, source_path)
            or not path_within(media_dir, os.path.dirname(final_path))
            or os.path.lexists(final_path)
        ):
            log_path_quarantine(
                purpose="bridge finalized publication",
                path=str(pending.get("source_path") or ""),
                root=bridge_dir,
                reason="source or destination failed final containment check",
            )
            return ""
        temp_path = ""
        try:
            try:
                os.link(source_path, final_path, follow_symlinks=False)
            except FileExistsError:
                raise
            except (OSError, TypeError, NotImplementedError):
                temp_name = f".{uuid.uuid4().hex}.bridge-publish.tmp"
                temp_path = resolve_under_root(
                    media_dir,
                    temp_name,
                    purpose="bridge finalized copy temp",
                )
                if not temp_path:
                    raise ValueError("Invalid bridge finalized copy temp path")
                shutil.copy2(source_path, temp_path)
                if os.path.lexists(final_path):
                    raise FileExistsError(final_path)
                atomic_replace(temp_path, final_path)
                temp_path = ""
        except (OSError, TypeError, NotImplementedError, ValueError) as exc:
            if temp_path:
                try:
                    if path_within(media_dir, temp_path) and os.path.isfile(temp_path):
                        os.remove(temp_path)
                except OSError:
                    logger.warning("Failed to remove bridge publication temp %s", temp_path)
            log_path_quarantine(
                purpose="bridge finalized publication",
                path=str(source_path or ""),
                root=bridge_dir,
                reason=f"publication failed: {exc}",
            )
            return ""
        if os.path.islink(final_path) or not os.path.isfile(final_path) or not path_within(media_dir, final_path):
            log_path_quarantine(
                purpose="bridge finalized publication",
                path=str(final_path or ""),
                root=media_dir,
                reason="published output failed media containment check",
            )
            try:
                if path_within(media_dir, os.path.dirname(final_path)) and os.path.lexists(final_path):
                    os.unlink(final_path)
            except OSError:
                logger.warning("Failed to remove rejected bridge output %s", final_path)
            return ""
        return final_path

    for rel_path in new_files:
        source_path = resolve_under_root(
            bridge_dir,
            rel_path,
            purpose="bridge output source file",
            must_exist=True,
        )
        if not source_path or os.path.islink(source_path) or not os.path.isfile(source_path):
            continue

        counter += 1
        source_basename = os.path.basename(source_path)
        ext = _safe_extension_from_filename(source_basename)
        target_basename = f"{naming_stem}_{counter:04d}{ext}"
        final_name = _reserve_final_name(target_basename)
        final_rel_path, final_path = _project_media_file(project, final_name, purpose="bridge finalized media path")

        pending_moves.append({
            "final_name": final_name,
            "final_rel_path": final_rel_path,
            "source_path": source_path,
            "source_basename": source_basename,
            "final_path": final_path,
            "rel_path": rel_path,
        })

    completed_moves = []
    for pending in pending_moves:
        moved_path = _publish_bridge_output_file(pending)
        if not moved_path:
            continue
        pending["final_path"] = moved_path
        asset_type, artifact_kind = classify_asset_path(moved_path)
        try:
            metadata = _extract_bridge_asset_metadata(moved_path, asset_type)
        except MediaProbeError as exc:
            logger.warning("Skipping unprobeable bridge media output %s: %s", pending["source_basename"], exc)
            try:
                if path_within(media_dir, moved_path) and os.path.isfile(moved_path):
                    os.remove(moved_path)
            except OSError:
                logger.warning("Failed to remove unregistered bridge output %s", moved_path)
            continue
        if asset_type == "video":
            _record_guide_bleed_if_needed(
                entry.get("guide_bleed_check") or {},
                int(metadata.get("frame_count", 0) or 0),
                "bridge",
            )
        generation_params = _generation_params_with_detected_workflow(
            dict(entry.get("generation_params") or {}),
            moved_path,
        )
        asset = Asset(
            name=pending["final_name"],
            asset_type=asset_type,
            artifact_kind=artifact_kind,
            path=pending["final_rel_path"],
            prompt=str(entry.get("prompt_text") or ""),
            generation_params=generation_params,
            width=metadata["width"],
            height=metadata["height"],
            frame_count=metadata["frame_count"],
            fps=metadata["fps"],
            duration_sec=metadata["duration_sec"],
            sample_rate=metadata["sample_rate"],
            has_audio=metadata["has_audio"],
            folder=target_folder,
        )
        asset, upgraded_placeholder = _upgrade_bridge_placeholder_asset(project, asset)
        if not upgraded_placeholder:
            project.add_asset(asset)
        pending["asset"] = asset
        pending["asset_type"] = asset_type
        registered_assets.append((pending["rel_path"], asset))
        completed_moves.append(pending)

    changed = bool(registered_assets)
    if target_folder and registered_assets:
        _ensure_asset_folder_metadata(project, target_folder)
        changed = True
    if entry.get("mark_queue_complete") and not queue_job_id:
        logger.warning(
            "Bridge mark_queue_complete=True but execution context had no queue_job_id; "
            "queue completion skipped. prompt=%s node=%s registered=%d. "
            "Any 'running' queue job will need to be inspected manually.",
            entry.get("prompt_key"),
            entry.get("bridge_node_id"),
            len(registered_assets),
        )
    elif registered_assets and entry.get("mark_queue_complete"):
        first_asset = registered_assets[0][1]
        changed = _mark_queue_job_completed(project, queue_job_id, first_asset.asset_id) or changed
    elif entry.get("mark_queue_complete") and not registered_assets:
        changed = _mark_queue_job_failed(project, queue_job_id, "Bridge terminal produced no files") or changed
    elif not registered_assets:
        logger.info(
            "Bridge produced no files for prompt=%s node=%s",
            entry.get("prompt_key"),
            entry.get("bridge_node_id"),
        )

    preview_expires_at = None
    retain_native_preview = bool(registered_assets) and entry.get("prompt_key_source") != "recovered"
    if retain_native_preview:
        preview_expires_at = time.time() + BRIDGE_PREVIEW_GRACE_SEC
        try:
            _write_bridge_sidecar(
                bridge_dir,
                _list_bridge_files(bridge_dir),
                preview_expires_at=preview_expires_at,
            )
        except Exception:
            _cleanup_bridge_output_dir(project, bridge_dir)
            raise

    try:
        if changed:
            committed_project = _save_generated_project(project, entry.get("base_modified_at", ""), created_ids=created_ids_since(_pre_item_ids, project))
            asset_id_remap = getattr(committed_project, "_asset_id_remap", {}) if committed_project is not None else {}
            for pending in completed_moves:
                if pending["asset_type"] in {"video", "image", "audio"}:
                    from ..server.thumbnail_service import ensure_thumbnail
                    asset = pending["asset"]
                    committed_asset_id = asset_id_remap.get(asset.asset_id, asset.asset_id)
                    thumb_path = _project_thumbnail_path(project, committed_asset_id, purpose="bridge asset thumbnail path")
                    if thumb_path:
                        ensure_thumbnail(pending["asset_type"], pending["final_path"], thumb_path)

        if retain_native_preview:
            refreshed_expires_at = time.time() + BRIDGE_PREVIEW_GRACE_SEC
            try:
                _write_bridge_sidecar(
                    bridge_dir,
                    _list_bridge_files(bridge_dir),
                    preview_expires_at=refreshed_expires_at,
                )
                preview_expires_at = refreshed_expires_at
            except Exception:
                logger.warning(
                    "Failed to refresh bridge preview grace after durable registration for %s",
                    bridge_dir,
                    exc_info=True,
                )

        logger.info(
            "Bridge finalize: prompt=%s node=%s published=%d target_folder=%s stem=%s",
            entry.get("prompt_key"),
            entry.get("bridge_node_id"),
            len(registered_assets),
            target_folder or "(root)",
            naming_stem,
        )
    finally:
        if preview_expires_at is not None:
            _schedule_bridge_preview_cleanup(project, bridge_dir, preview_expires_at)
        else:
            _cleanup_bridge_output_dir(project, bridge_dir)
    return [asset for _rel_path, asset in registered_assets]


def _finalize_prompt_bridges(prompt_key: str, source: str = "unknown") -> None:
    with _BRIDGE_REGISTRY_LOCK:
        entries = [
            dict(entry)
            for (_registry_prompt_key, _node_id), entry in list(_BRIDGE_REGISTRY.items())
            if _registry_prompt_key == prompt_key
        ]
        for registry_key in [key for key in _BRIDGE_REGISTRY.keys() if key[0] == prompt_key]:
            _BRIDGE_REGISTRY.pop(registry_key, None)
        _BRIDGE_PROMPT_WATCHERS.pop(prompt_key, None)
        _cleanup_prompt_key_state(prompt_key)

    if entries:
        logger.info(
            "Bridge finalize trigger: path=%s prompt=%s entries=%d",
            source,
            prompt_key,
            len(entries),
        )

    for entry in entries:
        try:
            _finalize_bridge_entry(entry)
        except Exception:
            logger.exception(
                "Bridge finalization failed for prompt=%s node=%s",
                entry.get("prompt_key"),
                entry.get("bridge_node_id"),
            )


def _watch_prompt_for_bridge_completion(prompt_key: str, prompt) -> None:
    started_at = time.time()
    quiet_since = None

    while time.time() - started_at < BRIDGE_MAX_WAIT_SEC:
        with _BRIDGE_REGISTRY_LOCK:
            entries = [dict(entry) for (registered_prompt_key, _node_id), entry in _BRIDGE_REGISTRY.items() if registered_prompt_key == prompt_key]
        if not entries:
            with _BRIDGE_REGISTRY_LOCK:
                _BRIDGE_PROMPT_WATCHERS.pop(prompt_key, None)
                _cleanup_prompt_key_state(prompt_key)
            return

        activity_points = [
            _latest_bridge_change_time(entry["bridge_dir"])
            for entry in entries
            if os.path.isdir(entry["bridge_dir"])
        ]
        latest_activity = max(activity_points) if activity_points else started_at
        running = _prompt_currently_running(prompt_key, prompt)
        now = time.time()
        fallback_wait_met = now - started_at >= BRIDGE_FALLBACK_MIN_WAIT_SEC

        if running:
            quiet_since = None
        else:
            if quiet_since is None:
                quiet_since = max(latest_activity, now)
            quiet_since = max(quiet_since, latest_activity)
            if fallback_wait_met and now - quiet_since >= BRIDGE_IDLE_SETTLE_SEC:
                break

        time.sleep(BRIDGE_POLL_INTERVAL_SEC)

    _finalize_prompt_bridges(prompt_key, source="watcher")


def _ensure_prompt_bridge_watcher(prompt_key: str, prompt) -> None:
    with _BRIDGE_REGISTRY_LOCK:
        watcher = _BRIDGE_PROMPT_WATCHERS.get(prompt_key)
        if watcher and watcher.is_alive():
            return
        watcher = threading.Thread(
            target=_watch_prompt_for_bridge_completion,
            args=(prompt_key, prompt),
            name=f"sonder-bridge-{prompt_key}",
            daemon=True,
        )
        _BRIDGE_PROMPT_WATCHERS[prompt_key] = watcher
        watcher.start()


_install_bridge_prompt_queue_hooks()


def _get_ffmpeg() -> str:
    """Return ffmpeg path, preferring imageio_ffmpeg if available."""
    return get_ffmpeg_path()


def _frames_to_tensor(frames: list[np.ndarray]) -> torch.Tensor:
    """Convert list of BGR uint8 frames to (N, H, W, 3) float32 RGB tensor."""
    rgb_frames = [cv2.cvtColor(f, cv2.COLOR_BGR2RGB) for f in frames]
    arr = np.stack(rgb_frames, axis=0).astype(np.float32) / 255.0
    return torch.from_numpy(arr)


def _tensor_to_frames(tensor: torch.Tensor) -> list[np.ndarray]:
    """Convert (N, H, W, 3) float32 RGB tensor to list of BGR uint8 frames."""
    arr = tensor_to_uint8_frames(tensor, mode="truncate")
    return [cv2.cvtColor(arr[i], cv2.COLOR_RGB2BGR) for i in range(arr.shape[0])]


def _save_audio_waveform(audio: dict, output_path: str) -> tuple[int, torch.Tensor]:
    """Persist a ComfyUI AUDIO dict as a waveform file and return sample rate plus waveform."""
    waveform = audio["waveform"]
    if waveform.dim() == 3:
        waveform = waveform.squeeze(0)
    sample_rate = int(audio["sample_rate"])
    write_audio_wav(output_path, waveform.detach().cpu().numpy(), sample_rate)
    return sample_rate, waveform


def _save_preview_thumbnail(frame_bgr: np.ndarray, prefix: str = "sonder_thumb") -> list[dict]:
    """Save a frame as a PNG thumbnail to ComfyUI's temp dir. Returns UI image list."""
    temp_dir = folder_paths.get_temp_directory()
    os.makedirs(temp_dir, exist_ok=True)

    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(rgb)

    suffix = ''.join(random.choice("abcdefghijklmnopqrstuvwxyz") for _ in range(5))
    filename = f"{prefix}_{suffix}.png"
    filepath = os.path.join(temp_dir, filename)
    img.save(filepath)

    return [{"filename": filename, "subfolder": "", "type": "temp"}]


class SonderSaveVideo:
    """Save IMAGE frames as a video file with optional audio."""

    CATEGORY = "Sonder/IO"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("output_path",)
    OUTPUT_TOOLTIPS = ("Absolute path to the saved video file.",)
    FUNCTION = "save_video"
    DESCRIPTION = "Encodes an IMAGE tensor to a project video asset. Optionally muxes audio. Shows a thumbnail preview of the first frame. Optionally auto-corrects VAE color drift using the render's protected context frames."

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "project": ("SONDER_PROJECT", {"tooltip": "The project — video saves to its exports/ folder."}),
                "frames": ("IMAGE", {"tooltip": "Batch of frames to encode as video."}),
                "filename_prefix": ("STRING", {"default": "output", "tooltip": "Prefix for the output filename."}),
                "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0, "step": 0.001, "tooltip": "Output video frame rate."}),
                "mode": (["Video", "Take"], {"default": "Video", "tooltip": "Video: normal save. Take: saves and auto-places result on timeline as a new lane."}),
                "place_audio_on_timeline": ("BOOLEAN", {"default": True, "tooltip": "Take mode only. When on, the take's audio is also placed as a separate audio track on the timeline. Turn off for fixed/unchanging audio you've already placed once, to avoid duplicate audio lanes and assets per take. The audio is still muxed into the saved video either way."}),
                "mark_queue_complete": ("BOOLEAN", {"default": False, "tooltip": "When enabled, this save node completes the active Sonder queue job after a successful save."}),
            },
            "optional": {
                "audio": ("AUDIO", {"tooltip": "Audio to mux into the video."}),
                "embed_metadata": ("BOOLEAN", {"default": True, "tooltip": "Embed source ComfyUI workflow metadata into files written by this save node."}),
                "save_preset": (SAVE_VIDEO_PRESET_ORDER, {"default": DEFAULT_SAVE_VIDEO_PRESET, "tooltip": SAVE_PRESET_TOOLTIP}),
                "custom_output_kind": (CUSTOM_OUTPUT_KIND_OPTIONS, {"default": CUSTOM_OUTPUT_KIND_VIDEO, "tooltip": "Custom only. Video File encodes one media asset; PNG Sequence writes frame images."}),
                "custom_container": (CUSTOM_CONTAINER_OPTIONS, {"default": "mp4", "tooltip": "Custom video only. Output container/extension."}),
                "custom_video_codec": (CUSTOM_VIDEO_CODEC_OPTIONS, {"default": "libx264", "tooltip": "Custom video only. ffmpeg video codec."}),
                "custom_pix_fmt": (CUSTOM_PIX_FMT_OPTIONS, {"default": "yuv420p", "tooltip": "Custom video only. ffmpeg pixel format."}),
                "custom_crf": ("INT", {"default": 18, "min": 0, "max": 51, "tooltip": "Custom x264/x265 only. Lower means higher quality and larger files."}),
                "custom_encoder_preset": (CUSTOM_ENCODER_PRESET_OPTIONS, {"default": "slow", "tooltip": "Custom x264/x265 only. Slower presets improve compression efficiency."}),
                "custom_audio_codec": (CUSTOM_AUDIO_CODEC_OPTIONS, {"default": "aac", "tooltip": "Custom video only. Choose none to omit connected audio."}),
                "custom_audio_bitrate_kbps": ("INT", {"default": 192, "min": 1, "max": 10000, "tooltip": "Custom AAC audio only. Audio bitrate in kbps."}),
                "custom_png_compression": ("INT", {"default": 0, "min": 0, "max": 9, "tooltip": "Custom PNG Sequence only. 0 is fastest/largest; 9 is smallest/slowest."}),
                "autoplay_preview": ("BOOLEAN", {"default": False, "tooltip": "Autoplay the inline video player on this node card after a run. When off, it opens paused on the first frame. Display-only — does not affect the saved file."}),
                "color_drift_correction": ("BOOLEAN", {"default": True, "tooltip": "Corrects accumulated VAE color drift by matching this render's untouched context frames back to the editor's pre-encode reference. Fitted per save; skipped automatically when no context exists or the correction would exceed safe bounds (outcome recorded in asset metadata)."}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "extra_pnginfo": "EXTRA_PNGINFO",
                "unique_id": "UNIQUE_ID",
            },
        }

    def save_video(self, project, frames, filename_prefix="output", fps=24.0,
                   mode="Video", place_audio_on_timeline=True, mark_queue_complete=False, audio=None,
                   save_preset=DEFAULT_SAVE_VIDEO_PRESET,
                   custom_output_kind=CUSTOM_OUTPUT_KIND_VIDEO,
                   custom_container="mp4",
                   custom_video_codec="libx264",
                   custom_pix_fmt="yuv420p",
                   custom_crf=18,
                    custom_encoder_preset="slow",
                    custom_audio_codec="aac",
                    custom_audio_bitrate_kbps=192,
                    custom_png_compression=0,
                    embed_metadata=True,
                    autoplay_preview=False,  # frontend display-only; accepted and ignored here
                    color_drift_correction=True,
                    prompt=None,
                    extra_pnginfo=None,
                    unique_id=None):
        # Save to media/ so it appears in the project's asset gallery
        media_dir = _require_project_media_root(project)
        # Baseline for the created-set hand-off: anything added to `project` below
        # is "generated this run". Pre-existing generated items the user deletes
        # mid-generation stay deleted (not resurrected) by the conflict-path merge.
        _pre_item_ids = snapshot_item_ids(project)

        preset_id = normalize_save_preset(save_preset)
        custom_options = {
            "custom_output_kind": custom_output_kind,
            "custom_container": custom_container,
            "custom_video_codec": custom_video_codec,
            "custom_pix_fmt": custom_pix_fmt,
            "custom_crf": custom_crf,
            "custom_encoder_preset": custom_encoder_preset,
            "custom_audio_codec": custom_audio_codec,
            "custom_audio_bitrate_kbps": custom_audio_bitrate_kbps,
            "custom_png_compression": custom_png_compression,
        }
        custom_spec = resolve_custom_export_options(custom_options) if preset_id == CUSTOM_SAVE_VIDEO_PRESET else None
        if preset_id == CUSTOM_SAVE_VIDEO_PRESET and custom_spec["output_kind"] == CUSTOM_OUTPUT_KIND_PNG_SEQUENCE and mode == "Take":
            raise ValueError("Custom PNG Sequence export is not available in Take mode. Choose Video mode or a video preset.")
        extension = output_extension_for_custom_options(custom_options) if custom_spec else output_extension_for_preset(preset_id)
        safe_prefix = _safe_output_stem(filename_prefix, "output")
        output_filename = f"{safe_prefix}_{uuid.uuid4().hex[:6]}{extension}"
        output_rel_path, output_path = _project_media_file(project, output_filename, purpose="save video output path")

        tensor_mode = custom_spec["tensor_mode"] if custom_spec else tensor_mode_for_preset(preset_id)
        execution_context = _copy_execution_context(project)
        # Drift correction fits on the mask-protected context frames the editor
        # stashed and applies fused into the uint8 conversion; `frames` itself is
        # never mutated (ComfyUI caches node outputs).
        drift_correction, drift_record = fit_drift_correction(
            frames, execution_context, enabled=bool(color_drift_correction)
        )
        if drift_correction is not None:
            logger.info(
                "color drift correction applied (%s): frames_used=%d mae %.3f -> %.3f",
                drift_record.get("mode", "global"),
                len(drift_record.get("frames_used") or []),
                drift_record["residual_before"]["mae"],
                drift_record["residual_after"]["mae"],
            )
            rgb_frames = transform_tensor_to_uint8_frames(frames, drift_correction, mode=tensor_mode)
        else:
            if bool(color_drift_correction):
                logger.info("color drift correction skipped: %s", drift_record.get("skip_reason"))
            rgb_frames = tensor_to_uint8_frames(frames, mode=tensor_mode)
        _record_guide_bleed_if_needed(
            _guide_bleed_check_from_context(
                execution_context, str(getattr(project, "project_id", "") or "")
            ),
            len(rgb_frames),
            "save_video_tensor",
        )
        h, w = rgb_frames[0].shape[:2]
        workflow = _workflow_from_extra_pnginfo(extra_pnginfo)
        embed_metadata_enabled = bool(embed_metadata)

        def build_asset_editor_export() -> dict:
            editor_export = _compact_editor_export(
                project,
                execution_context,
                has_embedded_workflow=embed_metadata_enabled and workflow is not None,
                workflow=workflow,
                prompt=prompt,
                consumer_unique_id=unique_id,
            )
            editor_export.update({
                "fps": fps,
                "resolution": {"width": w, "height": h},
                "preset": preset_id,
                "custom_encode": dict(custom_options) if custom_spec else None,
            })
            return editor_export

        asset_editor_export = build_asset_editor_export()
        file_metadata = (
            _file_metadata_payload(prompt, workflow, asset_editor_export)
            if embed_metadata_enabled and (prompt is not None or workflow is not None)
            else None
        )

        if custom_spec and custom_spec["output_kind"] == CUSTOM_OUTPUT_KIND_PNG_SEQUENCE:
            encode_metadata = metadata_for_save_preset(preset_id, custom_options)
            asset_metadata = dict(encode_metadata)
            asset_metadata["editor_export"] = asset_editor_export
            asset_metadata[COLOR_DRIFT_METADATA_KEY] = drift_record
            output_path, png_assets = _save_custom_png_sequence(
                project,
                rgb_frames,
                safe_prefix,
                asset_metadata,
                file_metadata=file_metadata,
            )
            if mark_queue_complete:
                result_asset_id = png_assets[0].asset_id if png_assets else ""
                _mark_queue_job_completed(project, str(execution_context.get("queue_job_id") or ""), result_asset_id)
            _save_generated_project(project, str(execution_context.get("base_modified_at") or ""), created_ids=created_ids_since(_pre_item_ids, project))
            preview_images = _save_preview_thumbnail(cv2.cvtColor(rgb_frames[0], cv2.COLOR_RGB2BGR), "sonder_savepng")
            logger.info("Saved PNG sequence to %s (%d frames)", output_path, len(rgb_frames))
            return {
                "ui": {"images": preview_images},
                "result": (output_path,),
            }

        audio_tmp = None
        if audio is not None:
            # Write the transient mux WAV to ComfyUI's temp dir, NOT media/, so the
            # editor's media-folder scan never ffprobes it mid-life — otherwise the
            # os.remove below races that probe handle and raises WinError 32 (sharing
            # violation), failing the save. Mirrors the Preview node, which already
            # writes its temp audio to get_temp_directory().
            tmp_root = folder_paths.get_temp_directory()
            os.makedirs(tmp_root, exist_ok=True)
            audio_tmp = os.path.join(tmp_root, f"_tmp_audio_{uuid.uuid4().hex[:6]}.wav")
            try:
                _save_audio_waveform(audio, audio_tmp)
            except Exception as e:
                logger.warning("Failed to save temp audio: %s", e)
                audio_tmp = None

        has_audio = bool(audio_tmp) and not (custom_spec and custom_spec["audio_codec"] == "none")
        encode_timeout = save_video_encode_timeout_seconds(
            preset_id,
            len(rgb_frames),
            w,
            h,
            custom_options if custom_spec else None,
        )
        ffmpeg_started_at = time.perf_counter()
        logger.info(
            "ffmpeg start: save_video output=%s preset=%s frames=%d audio=%s timeout=%ss",
            output_path,
            preset_id,
            len(rgb_frames),
            has_audio,
            encode_timeout,
        )
        # ComfyUI native progress bar — emits "progress" WS events keyed to this
        # executing SonderSaveVideo node. The editor's extension.js maps those
        # (filtered to Sonder save nodes) into the notification foreground pill.
        encode_pbar = None
        try:
            import comfy.utils
            encode_pbar = comfy.utils.ProgressBar(len(rgb_frames))
        except Exception:
            encode_pbar = None
        encode_progress_cb = None
        if encode_pbar is not None:
            _encode_total = len(rgb_frames)
            encode_progress_cb = lambda done: encode_pbar.update_absolute(min(int(done), _encode_total))
        try:
            encode_metadata = encode_video(
                rgb_frames,
                preset_id=preset_id,
                output_path=output_path,
                fps=fps,
                audio_path=audio_tmp,
                custom_options=custom_options if custom_spec else None,
                timeout=encode_timeout,
                embed_metadata=file_metadata,
                progress_callback=encode_progress_cb,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "ffmpeg timeout: save_video output=%s duration=%.2fs",
                output_path,
                time.perf_counter() - ffmpeg_started_at,
            )
            raise
        finally:
            if audio_tmp and os.path.isfile(audio_tmp):
                os.remove(audio_tmp)
        logger.info(
            "ffmpeg end: save_video output=%s preset=%s duration=%.2fs",
            output_path,
            preset_id,
            time.perf_counter() - ffmpeg_started_at,
        )
        asset_generation_params = dict(encode_metadata)
        asset_generation_params["editor_export"] = asset_editor_export
        asset_generation_params[COLOR_DRIFT_METADATA_KEY] = drift_record

        # Auto-register as a project asset
        total_frames = len(rgb_frames)
        asset = Asset(
            name=output_filename,
            asset_type="video",
            path=output_rel_path,
            width=w,
            height=h,
            frame_count=total_frames,
            fps=fps,
            duration_sec=total_frames / fps if fps > 0 else 0.0,
            has_audio=has_audio,
            generation_params=dict(asset_generation_params),
        )
        project.add_asset(asset)

        # Generate thumbnail
        from ..server.thumbnail_service import ensure_thumbnail
        thumb_path = _project_thumbnail_path(project, asset.asset_id, purpose="save video thumbnail path")
        if thumb_path:
            ensure_thumbnail("video", output_path, thumb_path)

        # --- Take mode: auto-place on timeline ---
        if mode == "Take" and hasattr(project, '_execution_context') and project._execution_context:
            ctx = project._execution_context
            scene = project.get_scene(ctx.get("scene_id", ""))
            if scene:
                # Organize asset into Takes folder
                asset.folder = f"Takes/{ctx.get('scene_name', scene.name)}"
                take_generation_params = {**_public_execution_context(ctx), **dict(asset_generation_params)}
                asset.generation_params = dict(take_generation_params)

                # Find next available video lane
                existing_lanes = [c.track_index for c in scene.clips] if scene.clips else [-1]
                new_lane = max(existing_lanes) + 1

                # Ensure scene has enough lanes
                if scene.video_lane_count <= new_lane:
                    scene.video_lane_count = new_lane + 1
                while len(scene.video_lane_configs) < scene.video_lane_count:
                    scene.video_lane_configs.append(LaneConfig())

                # Determine clip placement — at original selection, not context-expanded range
                def context_int(key, default=0):
                    try:
                        return int(ctx.get(key, default) or 0)
                    except (TypeError, ValueError):
                        return default

                sel_start = context_int("selection_start", 0)
                sel_end = context_int("selection_end", sel_start + total_frames)
                actual_pre = max(0, context_int(
                    "actual_pre_context_frames",
                    context_int("pre_context_frames", context_int("context_frames", 0)),
                ))
                actual_post = max(0, context_int(
                    "actual_post_context_frames",
                    context_int("post_context_frames", context_int("context_frames", 0)),
                ))
                frame_count_padding = max(0, context_int("frame_count_padding", 0))
                take_placement_mode = ctx.get("take_placement_mode", "trimmed")
                take_placement_linked = ctx.get("take_placement_linked", True) is not False
                take_placement_muted = bool(ctx.get("take_placement_muted", False))
                take_fit_mode = str(ctx.get("take_fit_mode", DEFAULT_FIT_MODE) or DEFAULT_FIT_MODE).strip().lower()
                if take_fit_mode not in FIT_MODES:
                    take_fit_mode = DEFAULT_FIT_MODE
                take_crop_position = str(ctx.get("take_crop_position", DEFAULT_CROP_POSITION) or DEFAULT_CROP_POSITION).strip().lower()
                if take_crop_position not in CROP_POSITIONS:
                    take_crop_position = DEFAULT_CROP_POSITION

                mask_pre = max(0, min(actual_pre, context_int("mask_pre_offset", 0)))
                mask_post = max(0, min(actual_post, context_int("mask_post_offset", 0)))

                if take_placement_mode == "untrimmed":
                    # DIAGNOSTIC: untrimmed mode intentionally ignores mask_pre/post_offset (Phase 1 decision).
                    # Places the full real rendered source (pre+generation+post) on the timeline so the seam is
                    # visible. Constraint padding is a synthetic tensor tail and must never become source media.
                    visible_source_frames = max(0, total_frames - frame_count_padding)
                    source_in_frame = 0
                    source_out_frame = visible_source_frames
                    timeline_start_frame = sel_start - actual_pre
                    timeline_end_frame = timeline_start_frame + visible_source_frames
                    source_origin_frame = 0
                    clip_total_source_frames = visible_source_frames
                else:
                    # Trimmed (default): align the visible region with the mask region the
                    # renderer actually denoised. mask_start_frame / mask_end_frame are the
                    # post-snap output-tensor coords published by SonderEditor; on the LTX
                    # 8n+1 grid they extend outward (start floors, end ceils) so the take
                    # must mirror that extension. Defaults fall back to the un-snapped
                    # offsets so behavior is unchanged when no template constraint applies.
                    gen_len = max(0, sel_end - sel_start)
                    mask_start_frame = max(0, context_int("mask_start_frame", actual_pre - mask_pre))
                    mask_end_frame = context_int(
                        "mask_end_frame",
                        actual_pre + gen_len + mask_post + frame_count_padding,
                    )
                    # Padding sits at the tail of the output tensor and is never user-visible
                    # content; subtracting it preserves prior trimmed placement when snap is a
                    # no-op while still extending the take by the snap delta when present.
                    visible_source_end = max(mask_start_frame, min(
                        mask_end_frame - frame_count_padding,
                        total_frames - frame_count_padding,
                    ))
                    context_start_scene_frame = sel_start - actual_pre
                    source_in_frame = mask_start_frame
                    source_out_frame = visible_source_end
                    timeline_start_frame = context_start_scene_frame + mask_start_frame
                    timeline_end_frame = context_start_scene_frame + visible_source_end
                    source_origin_frame = 0
                    clip_total_source_frames = max(0, total_frames - frame_count_padding)

                clip = ClipReference(
                    source_path=output_rel_path,
                    timeline_start_frame=timeline_start_frame,
                    timeline_end_frame=timeline_end_frame,
                    source_in_frame=source_in_frame,
                    source_out_frame=source_out_frame,
                    total_source_frames=clip_total_source_frames,
                    source_origin_frame=source_origin_frame,
                    track_index=new_lane,
                    muted=take_placement_muted,
                    fit_mode=take_fit_mode,
                    crop_position=take_crop_position,
                    is_generated=True,
                    generation_params=dict(take_generation_params),
                    take_metadata=dict(take_generation_params),
                )
                scene.clips.append(clip)
                placed_audio_track = None
                if place_audio_on_timeline and has_audio and audio is not None:
                    audio_filename = f"{os.path.splitext(output_filename)[0]}_audio.wav"
                    audio_rel_path, audio_abs_path = _project_media_file(project, audio_filename, purpose="take audio output path")
                    try:
                        sample_rate, waveform = _save_audio_waveform(audio, audio_abs_path)
                        audio_duration_sec = waveform.shape[-1] / sample_rate if sample_rate > 0 else 0.0
                        audio_asset = Asset(
                            name=f"{output_filename} (audio)",
                            asset_type="audio",
                            path=audio_rel_path,
                            duration_sec=audio_duration_sec,
                            sample_rate=sample_rate,
                            folder=asset.folder,
                            generation_params=dict(take_generation_params),
                        )
                        project.add_asset(audio_asset)

                        audio_thumb_path = _project_thumbnail_path(project, audio_asset.asset_id, purpose="take audio thumbnail path")
                        if audio_thumb_path:
                            ensure_thumbnail("audio", audio_abs_path, audio_thumb_path)

                        existing_audio_lanes = [track.lane_index for track in scene.audio_tracks] if scene.audio_tracks else [-1]
                        new_audio_lane = max(existing_audio_lanes) + 1
                        if scene.audio_lane_count <= new_audio_lane:
                            scene.audio_lane_count = new_audio_lane + 1
                        while len(scene.audio_lane_configs) < scene.audio_lane_count:
                            scene.audio_lane_configs.append(LaneConfig())

                        visible_len = source_out_frame - source_in_frame
                        assert (timeline_end_frame - timeline_start_frame) == visible_len, (
                            f"audio invariant: timeline span {timeline_end_frame - timeline_start_frame} != visible_len {visible_len}"
                        )
                        assert clip_total_source_frames == max(0, total_frames - frame_count_padding), (
                            f"audio invariant: total_source_frames {clip_total_source_frames} != total_frames - padding {max(0, total_frames - frame_count_padding)}"
                        )

                        placed_audio_track = AudioTrack(
                            source_path=audio_rel_path,
                            timeline_start_frame=timeline_start_frame,
                            timeline_end_frame=timeline_end_frame,
                            source_in_frame=source_in_frame,
                            total_source_frames=clip_total_source_frames,
                            source_origin_frame=source_origin_frame,
                            muted=take_placement_muted,
                            lane_index=new_audio_lane,
                        )
                        scene.audio_tracks.append(placed_audio_track)
                        logger.info("Take audio auto-placed on lane %d at frames %d-%d", new_audio_lane, timeline_start_frame, timeline_end_frame)
                    except Exception as e:
                        logger.warning("Take mode audio auto-placement failed for %s: %s", output_filename, e)
                if take_placement_linked and placed_audio_track is not None:
                    scene.linked_item_groups.append({
                        "group_id": uuid.uuid4().hex[:8],
                        "items": [
                            {"type": "clip", "id": clip.clip_id},
                            {"type": "audio", "id": placed_audio_track.track_id},
                        ],
                    })
                logger.info(
                    "Take auto-placed on lane %d at frames %d-%d (mode=%s linked=%s muted=%s)",
                    new_lane, timeline_start_frame, timeline_end_frame,
                    take_placement_mode, take_placement_linked, take_placement_muted,
                )
            else:
                logger.warning("Take mode: scene_id '%s' not found, skipping auto-placement", ctx.get("scene_id", ""))

        if mark_queue_complete:
            ctx = _copy_execution_context(project)
            _mark_queue_job_completed(project, str(ctx.get("queue_job_id") or ""), asset.asset_id)

        _save_generated_project(project, str(execution_context.get("base_modified_at") or ""), created_ids=created_ids_since(_pre_item_ids, project))

        # Inline node player descriptor — the saved project media file is served via
        # ComfyUI /view from <output>/sonder-projects/<dir>/media. Poster is the first
        # frame shown before playback starts. PNG-sequence saves keep the thumbnail-only
        # path above; this is the playable-video case.
        poster = _save_preview_thumbnail(cv2.cvtColor(rgb_frames[0], cv2.COLOR_RGB2BGR), "sonder_savevid")
        project_subfolder = f"sonder-projects/{os.path.basename(os.path.normpath(project.project_dir))}/media"

        logger.info("Saved video to %s (%d frames, %.1f fps, preset=%s)", output_path, len(rgb_frames), fps, preset_id)
        return {
            "ui": {
                "sonder_video": [{
                    "filename": output_filename,
                    "subfolder": project_subfolder,
                    "type": "output",
                    "fps": fps,
                    "has_audio": has_audio,
                    "poster": poster[0] if poster else None,
                }],
            },
            "result": (output_path,),
        }


class SonderSaveBridge:
    """Expose a prompt-isolated project output directory to external save nodes."""

    CATEGORY = "Sonder/IO"
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("output_dir", "filename_prefix")
    OUTPUT_TOOLTIPS = (
        "Absolute prompt-isolated directory for save nodes that expect a folder path. Files written here are renamed to the configured stem on registration.",
        "ComfyUI-relative filename prefix encoding the configured stem. Native save nodes that honor filename_prefix (SaveImage et al.) produce filenames like <stem>_00001_.ext directly.",
    )
    FUNCTION = "prepare_output"
    DESCRIPTION = "Creates a prompt-isolated output target inside the project cache. External save nodes write there, then the bridge registers the results into the Sonder asset system after the prompt settles."

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "project": ("SONDER_PROJECT", {"tooltip": "The project that owns the bridge output directory."}),
                "target_folder": ([""], {"default": "", "tooltip": "Optional asset folder label. Existing labels may be reused and new typed labels are created on registration."}),
                "prefix": ("STRING", {"default": "", "tooltip": "Optional filename prefix prepended to all outputs. Leave empty to skip."}),
                "mark_queue_complete": ("BOOLEAN", {"default": False, "tooltip": "When enabled, the bridge completes the active Sonder queue job after it successfully registers one or more files."}),
            },
            "hidden": {
                "prompt": "PROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    def prepare_output(self, project, target_folder="", prefix="", mark_queue_complete=False, prompt=None, unique_id=None):
        prompt_key, prompt_key_source = _resolve_bridge_prompt_key(prompt)
        bridge_node_id = str(unique_id or uuid.uuid4().hex[:8])
        execution_context = _copy_execution_context(project)
        generation_params = _bridge_generation_params(project, execution_context, prompt=prompt, consumer_unique_id=bridge_node_id)
        naming_stem = _build_bridge_naming_stem(project, execution_context, prefix)
        output_dir, filename_prefix = _prepare_bridge_output_dir(project, prompt_key, bridge_node_id, naming_stem)

        entry = {
            "project_dir": project.project_dir,
            "prompt_key": prompt_key,
            "bridge_node_id": bridge_node_id,
            "bridge_dir": output_dir,
            "prompt_key_source": prompt_key_source,
            "target_folder": _normalize_asset_folder(target_folder),
            "mark_queue_complete": bool(mark_queue_complete),
            "queue_job_id": str(execution_context.get("queue_job_id") or ""),
            "prompt_text": "",
            "generation_params": generation_params,
            "naming_stem": naming_stem,
            "base_modified_at": str(execution_context.get("base_modified_at") or ""),
            "guide_bleed_check": _guide_bleed_check_from_context(
                execution_context, str(getattr(project, "project_id", "") or "")
            ),
        }

        with _BRIDGE_REGISTRY_LOCK:
            _BRIDGE_REGISTRY[(prompt_key, bridge_node_id)] = entry

        _ensure_prompt_bridge_watcher(prompt_key, prompt)
        return (output_dir, filename_prefix)


class SonderPreviewVideo:
    """Preview video frames directly in ComfyUI's built-in viewer."""

    CATEGORY = "Sonder/IO"
    OUTPUT_NODE = True
    RETURN_TYPES = ()
    FUNCTION = "preview"
    DESCRIPTION = "Encodes frames to a temporary video for in-UI preview playback."

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "frames": ("IMAGE", {"tooltip": "Batch of frames to preview."}),
                "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0, "step": 0.001, "tooltip": "Playback frame rate for the preview."}),
            },
            "optional": {
                "audio": ("AUDIO", {"tooltip": "Optional audio to mux into the preview so playback has sound."}),
                "autoplay_preview": ("BOOLEAN", {"default": False, "tooltip": "Autoplay the inline video player on this node card after a run. When off, it opens paused on the first frame."}),
            },
        }

    def preview(self, frames, fps=24.0, audio=None, autoplay_preview=False):
        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)

        # Encode through the shared media_helpers path (streaming frames, browser-playable
        # Compatible MP4, audio mux, size-aware timeout) — the same encoder SonderSaveVideo
        # uses — so the preview is consistent and never builds a whole raw-video payload.
        preset_id = DEFAULT_SAVE_VIDEO_PRESET
        extension = output_extension_for_preset(preset_id)
        preview_filename = f"sonder_preview_{uuid.uuid4().hex[:8]}{extension}"
        preview_path = os.path.join(temp_dir, preview_filename)

        rgb_frames = tensor_to_uint8_frames(frames, mode=tensor_mode_for_preset(preset_id))
        h, w = rgb_frames[0].shape[:2]

        audio_tmp = None
        if audio is not None:
            audio_tmp = os.path.join(temp_dir, f"_tmp_preview_audio_{uuid.uuid4().hex[:6]}.wav")
            try:
                _save_audio_waveform(audio, audio_tmp)
            except Exception as e:
                logger.warning("Failed to save temp preview audio: %s", e)
                audio_tmp = None
        has_audio = bool(audio_tmp)

        encode_timeout = save_video_encode_timeout_seconds(preset_id, len(rgb_frames), w, h)
        ffmpeg_started_at = time.perf_counter()
        logger.info(
            "ffmpeg start: preview output=%s frames=%d audio=%s timeout=%ss",
            preview_path,
            len(rgb_frames),
            has_audio,
            encode_timeout,
        )
        try:
            encode_video(
                rgb_frames,
                preset_id=preset_id,
                output_path=preview_path,
                fps=fps,
                audio_path=audio_tmp,
                timeout=encode_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "ffmpeg timeout: preview output=%s duration=%.2fs",
                preview_path,
                time.perf_counter() - ffmpeg_started_at,
            )
            raise
        finally:
            if audio_tmp and os.path.isfile(audio_tmp):
                os.remove(audio_tmp)
        logger.info(
            "ffmpeg end: preview output=%s duration=%.2fs",
            preview_path,
            time.perf_counter() - ffmpeg_started_at,
        )

        # First-frame poster for the inline player (shown before playback starts).
        poster = _save_preview_thumbnail(cv2.cvtColor(rgb_frames[0], cv2.COLOR_RGB2BGR), "sonder_preview")

        return {"ui": {
            "sonder_video": [{
                "filename": preview_filename,
                "subfolder": "",
                "type": "temp",
                "fps": fps,
                "has_audio": has_audio,
                "poster": poster[0] if poster else None,
            }],
        }}
