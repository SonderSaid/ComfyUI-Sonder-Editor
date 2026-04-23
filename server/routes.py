import json
import logging
import os
import shutil
import uuid
from datetime import datetime, timedelta

from aiohttp import web

from .project_manager import create_project, load_project, save_project, list_projects
from .timeline_state import (
    TimelineProject, Asset, Scene, GuideFrame, PromptSection, AudioTrack,
    ClipReference, LaneConfig, GenerationJob, classify_asset_path,
)
from .thumbnail_service import ensure_thumbnail, generate_thumbnail_strip, generate_waveform_data

logger = logging.getLogger("sonder_editor")

# Defer route registration until ComfyUI's PromptServer is available.
try:
    from server import PromptServer
    routes = PromptServer.instance.routes
except Exception:
    routes = None
    logger.warning("PromptServer not available — Sonder Editor API routes disabled")


def _json_error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def _coerce_nonnegative_int(value, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


TRASH_RETENTION_DAYS = 30


def _resolve_source_path(source_path: str) -> str:
    source_path = str(source_path or "")
    if source_path and os.path.isfile(source_path):
        return source_path

    try:
        import folder_paths
        input_path = os.path.join(folder_paths.get_input_directory(), source_path)
        if os.path.isfile(input_path):
            return input_path
    except Exception:
        pass

    return source_path


def _detect_asset_type(source_path: str, fallback: str = "video") -> str:
    asset_type, _artifact_kind = classify_asset_path(source_path)
    return asset_type or fallback


def _classify_asset_for_registration(source_path: str) -> tuple[str, str]:
    return classify_asset_path(source_path)


def _extract_asset_media_metadata(source_path: str, asset_type: str) -> dict:
    width, height, frame_count, fps, duration_sec, sample_rate = 0, 0, 0, 0.0, 0.0, 0

    if asset_type == "video":
        try:
            import cv2
            cap = cv2.VideoCapture(source_path)
            if cap.isOpened():
                width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
                duration_sec = frame_count / fps if fps > 0 else 0.0
                cap.release()
        except Exception as e:
            logger.warning("Failed to extract video metadata: %s", e)
    elif asset_type == "image":
        try:
            from PIL import Image
            img = Image.open(source_path)
            width, height = img.size
        except Exception as e:
            logger.warning("Failed to extract image metadata: %s", e)
    elif asset_type == "audio":
        duration_sec = _get_audio_duration(source_path)

    has_audio = _video_has_audio(source_path) if asset_type == "video" else False
    return {
        "width": width,
        "height": height,
        "frame_count": frame_count,
        "fps": fps,
        "duration_sec": duration_sec,
        "sample_rate": sample_rate,
        "has_audio": has_audio,
    }


def _asset_file_size(source_path: str) -> int:
    try:
        return os.path.getsize(source_path)
    except OSError:
        return 0


def _asset_abspath(project: TimelineProject, asset: Asset) -> str:
    return os.path.join(project.project_dir, asset.path) if getattr(asset, "path", "") else ""


def _asset_missing(project: TimelineProject, asset: Asset) -> bool:
    source_path = _asset_abspath(project, asset)
    return not source_path or not os.path.isfile(source_path)


def _asset_payload(project: TimelineProject, asset: Asset) -> dict:
    payload = asset.to_dict()
    source_path = _asset_abspath(project, asset)
    thumb_path = os.path.join(
        project.project_dir, "cache", "thumbnails",
        f"{asset.asset_id}.png"
    )
    payload["has_thumbnail"] = os.path.isfile(thumb_path)
    payload["missing"] = _asset_missing(project, asset)
    payload["trashed_at"] = getattr(asset, "trashed_at", "") or ""
    payload["size_bytes"] = _asset_file_size(source_path) if source_path else 0
    payload["extension"] = os.path.splitext(getattr(asset, "path", "") or "")[1].lower()
    return payload


def _get_audio_duration(filepath: str) -> float:
    """Get audio duration in seconds using multiple fallback methods."""
    # Method 1: mutagen auto-detect
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(filepath)
        if mf is not None and mf.info and mf.info.length and mf.info.length > 0:
            logger.info("Audio duration via mutagen: %.2fs (%s)", mf.info.length, os.path.basename(filepath))
            return float(mf.info.length)
    except Exception as e:
        logger.debug("mutagen auto-detect failed for %s: %s", filepath, e)

    # Method 2: mutagen with explicit format (handles MP3 without ID3 tags)
    ext = os.path.splitext(filepath)[1].lower()
    try:
        if ext == ".mp3":
            from mutagen.mp3 import MP3
            m = MP3(filepath)
            if m.info and m.info.length > 0:
                logger.info("Audio duration via mutagen.mp3: %.2fs (%s)", m.info.length, os.path.basename(filepath))
                return float(m.info.length)
        elif ext == ".flac":
            from mutagen.flac import FLAC
            m = FLAC(filepath)
            if m.info and m.info.length > 0:
                logger.info("Audio duration via mutagen.flac: %.2fs (%s)", m.info.length, os.path.basename(filepath))
                return float(m.info.length)
        elif ext == ".ogg":
            from mutagen.oggvorbis import OggVorbis
            m = OggVorbis(filepath)
            if m.info and m.info.length > 0:
                return float(m.info.length)
        elif ext in (".m4a", ".aac"):
            from mutagen.mp4 import MP4
            m = MP4(filepath)
            if m.info and m.info.length > 0:
                return float(m.info.length)
    except Exception as e:
        logger.debug("mutagen explicit format failed for %s: %s", filepath, e)

    # Method 3: torchaudio
    try:
        import torchaudio
        waveform, sr = torchaudio.load(filepath)
        dur = waveform.shape[-1] / sr
        logger.info("Audio duration via torchaudio: %.2fs (%s)", dur, os.path.basename(filepath))
        return dur
    except Exception as e:
        logger.debug("torchaudio failed for %s: %s", filepath, e)

    # Method 4: ffprobe
    try:
        import subprocess
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            dur = float(result.stdout.strip())
            logger.info("Audio duration via ffprobe: %.2fs (%s)", dur, os.path.basename(filepath))
            return dur
    except Exception as e:
        logger.debug("ffprobe failed for %s: %s", filepath, e)

    logger.warning("All audio duration methods failed for %s", filepath)
    return 0.0


def _video_has_audio(filepath: str) -> bool:
    """Check if a video file contains an audio stream."""
    # Method 1: ffprobe
    try:
        import subprocess
        ffprobe = _find_ffprobe()
        result = subprocess.run(
            [ffprobe, "-v", "quiet", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and "audio" in result.stdout:
            return True
    except Exception as e:
        logger.debug("ffprobe audio check failed for %s: %s", filepath, e)

    # Method 2: mutagen (reads container metadata for audio streams)
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(filepath)
        if mf is not None and mf.info:
            # MP4/M4V containers: check for audio bitrate
            if hasattr(mf.info, 'bitrate') and hasattr(mf.info, 'codec'):
                return True
            # Most video containers with audio will have sample_rate
            if hasattr(mf.info, 'sample_rate') and mf.info.sample_rate > 0:
                return True
    except Exception as e:
        logger.debug("mutagen audio check failed for %s: %s", filepath, e)

    # Method 3: cv2 — check if video has more frames than expected for pure video
    # This is a heuristic; cv2 can't directly detect audio streams
    # but we can try loading audio with torchaudio as a last resort
    try:
        import torchaudio
        info = torchaudio.info(filepath)
        if info.num_frames > 0:
            return True
    except Exception:
        pass

    return False


def _find_ffmpeg() -> str:
    """Find ffmpeg executable, checking PATH, Python packages, and common locations."""
    import shutil
    path = shutil.which("ffmpeg")
    if path:
        logger.info("Found ffmpeg on PATH: %s", path)
        return path

    # Method 1: imageio-ffmpeg (bundled ffmpeg in Python package)
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            logger.info("Found ffmpeg via imageio-ffmpeg: %s", path)
            return path
    except ImportError:
        # Try to install imageio-ffmpeg (it bundles a static ffmpeg binary)
        logger.info("imageio-ffmpeg not found, attempting to install...")
        try:
            import subprocess, sys
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "imageio-ffmpeg"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120,
            )
            import imageio_ffmpeg
            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path and os.path.isfile(path):
                logger.info("Installed imageio-ffmpeg, ffmpeg at: %s", path)
                return path
        except Exception as e:
            logger.warning("Failed to install imageio-ffmpeg: %s", e)
    except Exception:
        pass

    # Method 2: Search common locations
    import sys
    candidates = []
    python_dir = os.path.dirname(sys.executable)
    candidates.append(os.path.join(python_dir, "ffmpeg.exe"))
    candidates.append(os.path.join(python_dir, "Scripts", "ffmpeg.exe"))
    candidates.append(os.path.join(python_dir, "..", "ffmpeg.exe"))
    # Stability Matrix common paths
    try:
        import folder_paths
        comfy_base = folder_paths.base_path
        candidates.append(os.path.join(comfy_base, "ffmpeg.exe"))
        candidates.append(os.path.join(comfy_base, "ffmpeg", "ffmpeg.exe"))
        # Go up from ComfyUI to Stability Data
        sm_data = os.path.dirname(os.path.dirname(comfy_base))
        candidates.append(os.path.join(sm_data, "Assets", "ffmpeg", "ffmpeg.exe"))
        candidates.append(os.path.join(sm_data, "Assets", "ffmpeg", "bin", "ffmpeg.exe"))
    except Exception:
        pass
    # Search imageio_ffmpeg package directory directly
    site_packages = os.path.join(python_dir, "Lib", "site-packages", "imageio_ffmpeg", "binaries")
    if os.path.isdir(site_packages):
        for f in os.listdir(site_packages):
            if "ffmpeg" in f.lower() and not "probe" in f.lower():
                candidates.append(os.path.join(site_packages, f))
    # Common system locations
    candidates.append(r"C:\ffmpeg\bin\ffmpeg.exe")
    candidates.append(os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe"))

    for c in candidates:
        if os.path.isfile(c):
            logger.info("Found ffmpeg at: %s", c)
            return c

    logger.warning("ffmpeg not found anywhere. Audio extraction from video will not work.")
    return "ffmpeg"  # fallback to PATH (will fail)


_ffmpeg_path = None

def _get_ffmpeg() -> str:
    global _ffmpeg_path
    if _ffmpeg_path is None:
        _ffmpeg_path = _find_ffmpeg()
    return _ffmpeg_path


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
        import subprocess

        ffmpeg = _get_ffmpeg()
        result = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-loglevel",
                "error",
                "-i",
                video_path,
                "-vf",
                f"select=eq(n\\,{frame_index})",
                "-frames:v",
                "1",
                "-pix_fmt",
                "rgb24",
                output_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0 or not os.path.isfile(output_path):
            logger.warning(
                "ffmpeg frame extraction failed for %s frame %s: %s",
                os.path.basename(video_path),
                frame_index,
                (result.stderr or "").strip()[:200],
            )
            return None
        return _read_image_size(output_path)
    except Exception as e:
        logger.warning("ffmpeg frame extraction failed for %s: %s", video_path, e)
        return None


def _find_ffprobe() -> str:
    """Find ffprobe executable."""
    import shutil
    path = shutil.which("ffprobe")
    if path:
        return path
    # Try same directory as ffmpeg
    ffmpeg = _get_ffmpeg()
    if ffmpeg and ffmpeg != "ffmpeg":
        probe = os.path.join(os.path.dirname(ffmpeg), "ffprobe" + (".exe" if os.name == "nt" else ""))
        if os.path.isfile(probe):
            return probe
    return "ffprobe"


def _extract_audio_from_video(video_path: str, output_path: str) -> bool:
    """Extract audio track from video file as WAV."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    # Method 1: ffmpeg
    try:
        import subprocess
        ffmpeg = _get_ffmpeg()
        result = subprocess.run(
            [ffmpeg, "-y", "-i", video_path, "-vn",
             "-acodec", "pcm_s16le", "-ar", "44100", output_path],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and os.path.isfile(output_path):
            logger.info("Extracted audio from video via ffmpeg: %s", os.path.basename(video_path))
            return True
        else:
            logger.warning("ffmpeg returned %d for %s: %s", result.returncode,
                           os.path.basename(video_path), result.stderr[:200] if result.stderr else "")
    except Exception as e:
        logger.warning("ffmpeg audio extraction failed for %s: %s", video_path, e)

    # Method 2: torchaudio (can read audio from video containers)
    try:
        import torchaudio
        waveform, sr = torchaudio.load(video_path)
        torchaudio.save(output_path, waveform, sr, format="wav")
        if os.path.isfile(output_path):
            logger.info("Extracted audio from video via torchaudio: %s", os.path.basename(video_path))
            return True
    except Exception as e:
        logger.debug("torchaudio audio extraction failed for %s: %s", video_path, e)

    return False


def _sync_media_folder(project: TimelineProject) -> bool:
    """Scan media/ folder for files not yet in the asset registry and add them.

    Returns True if any changes were made (new assets discovered or repaired).
    """
    changed = _purge_expired_trashed_assets(project)
    media_dir = os.path.join(project.project_dir, "media")
    if not os.path.isdir(media_dir):
        return changed

    # Build set of known relative paths
    known_paths = {a.path for a in project.assets}
    for filename in os.listdir(media_dir):
        filepath = os.path.join(media_dir, filename)
        if not os.path.isfile(filepath):
            continue

        rel_path = os.path.join("media", filename)
        if rel_path in known_paths:
            continue

        asset_type, artifact_kind = _classify_asset_for_registration(filename)

        # Extract metadata
        metadata = _extract_asset_media_metadata(filepath, asset_type)

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
        )
        project.add_asset(asset)

        if asset_type in {"video", "image", "audio"}:
            thumb_path = os.path.join(
                project.project_dir, "cache", "thumbnails",
                f"{asset.asset_id}.png"
            )
            ensure_thumbnail(asset_type, filepath, thumb_path)

        changed = True
        logger.info("Auto-registered asset: %s (%s)", filename, asset_type)

    # Repair video assets missing has_audio detection
    for asset in project.assets:
        if asset.asset_type == "video" and not asset.has_audio:
            filepath = os.path.join(project.project_dir, asset.path)
            if os.path.isfile(filepath) and _video_has_audio(filepath):
                asset.has_audio = True
                changed = True
                logger.info("Detected audio in video: %s", asset.name)

    # Repair existing audio assets with missing duration
    repaired_assets = {}
    for asset in project.assets:
        if asset.asset_type == "audio" and asset.duration_sec <= 0:
            filepath = os.path.join(project.project_dir, asset.path)
            if not os.path.isfile(filepath):
                continue
            dur = _get_audio_duration(filepath)
            if dur and dur > 0:
                asset.duration_sec = dur
                repaired_assets[asset.path] = dur
                changed = True
                logger.info("Repaired audio duration: %.2fs (%s)", dur, asset.name)

    # Repair audio tracks with 1-frame duration (caused by previous 0-duration bug)
    if repaired_assets:
        fps = project.fps or 24.0
        for scene in project.scenes:
            for track in scene.audio_tracks:
                duration_frames = track.timeline_end_frame - track.timeline_start_frame
                if duration_frames <= 1 and track.source_path in repaired_assets:
                    new_duration = int(repaired_assets[track.source_path] * fps)
                    if new_duration > 1:
                        track.timeline_end_frame = track.timeline_start_frame + new_duration
                        logger.info("Repaired audio track duration: %d frames (%s)", new_duration, track.source_path)

    return changed



def _get_base_dir() -> str:
    """Get the default base directory for projects."""
    try:
        import folder_paths
        return os.path.join(folder_paths.get_output_directory(), "sonder-projects")
    except Exception:
        return ""


def _load_project_from_request(request: web.Request) -> TimelineProject:
    """Load project from project_id path parameter."""
    project_id = request.match_info.get("project_id", "")
    project_dir = request.query.get("path", "")
    if not project_dir:
        base_dir = _get_base_dir()
        if base_dir:
            # Try to find by project_id in base_dir
            for entry in os.listdir(base_dir):
                entry_path = os.path.join(base_dir, entry)
                pfile = os.path.join(entry_path, "project.json")
                if os.path.isfile(pfile):
                    try:
                        with open(pfile, "r") as f:
                            data = json.load(f)
                        if data.get("project_id", "") == project_id or entry == project_id:
                            project_dir = entry_path
                            break
                    except Exception:
                        continue
    if not project_dir:
        raise FileNotFoundError(f"Project not found: {project_id}")
    project = load_project(project_dir)

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
        save_project(project)

    return project


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
        "active_scene": active_scene_payload,
    }


def _normalize_asset_folder(folder: str) -> str:
    return str(folder or "").strip().replace("\\", "/").strip("/")


def _query_flag(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _asset_is_trashed(asset: Asset) -> bool:
    return bool(getattr(asset, "trashed_at", ""))


def _project_trashed_assets(project: TimelineProject) -> list[Asset]:
    return [asset for asset in project.assets if _asset_is_trashed(asset)]


def _trash_project_asset(asset: Asset) -> dict:
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
    thumb_path = os.path.join(project.project_dir, "cache", "thumbnails", f"{asset.asset_id}.png")
    strip_path = os.path.join(project.project_dir, "cache", "thumbnails", f"{asset.asset_id}_strip.jpg")
    strip_info_path = strip_path + ".json"
    waveform_path = os.path.join(project.project_dir, "cache", "waveforms", f"{asset.asset_id}.json")
    extracted_audio_path = os.path.join(project.project_dir, "cache", "waveforms", f"{asset.asset_id}_audio.wav")
    for cache_path in [thumb_path, strip_path, strip_info_path, waveform_path, extracted_audio_path]:
        if os.path.isfile(cache_path):
            try:
                os.remove(cache_path)
            except OSError:
                logger.warning("Failed to clear asset cache file: %s", cache_path)


def _delete_asset_source_file(project: TimelineProject, asset: Asset, excluded_asset_ids: set[str] | None = None) -> None:
    source_path = _asset_abspath(project, asset)
    if not source_path or not os.path.isfile(source_path):
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


def _purge_expired_trashed_assets(project: TimelineProject) -> bool:
    changed = False
    for asset in list(project.assets):
        trashed_at = str(getattr(asset, "trashed_at", "") or "").strip()
        if not trashed_at:
            continue
        try:
            parsed = datetime.fromisoformat(trashed_at)
        except ValueError:
            continue
        compare_now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        if parsed <= (compare_now - timedelta(days=TRASH_RETENTION_DAYS)):
            _delete_project_asset(project, asset)
            changed = True
    return changed


def _trash_project_asset_folder(project: TimelineProject, folder: str) -> tuple[list[str], list[Asset]]:
    current = _normalize_asset_folder(folder)
    if not current:
        raise ValueError("Folder is required")

    all_folders = _collect_asset_folders(project)
    descendants = _folder_descendants(current, all_folders)
    if not descendants:
        raise FileNotFoundError(f"Folder not found: {current}")

    assets_to_trash = _find_assets_in_folder(project, current)
    for asset in assets_to_trash:
        _trash_project_asset(asset)

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


def _replace_project_asset(project: TimelineProject, asset: Asset, source_path: str) -> Asset:
    resolved_source = _resolve_source_path(source_path)
    if not resolved_source or not os.path.isfile(resolved_source):
        raise FileNotFoundError(f"File not found: {source_path}")

    replacement_type, replacement_artifact_kind = _classify_asset_for_registration(resolved_source)
    if replacement_type != asset.asset_type:
        raise ValueError(f"Replacement type mismatch: expected {asset.asset_type}, got {replacement_type}")

    media_dir = os.path.join(project.project_dir, "media")
    os.makedirs(media_dir, exist_ok=True)

    old_rel_path = asset.path
    old_abs_path = _asset_abspath(project, asset)
    old_ext = os.path.splitext(old_rel_path or "")[1].lower()
    new_ext = os.path.splitext(resolved_source)[1].lower()

    if old_rel_path and old_ext and old_ext == new_ext:
        next_rel_path = old_rel_path
    else:
        next_rel_path = os.path.join("media", f"{uuid.uuid4().hex[:8]}_{os.path.basename(resolved_source)}")
    next_abs_path = os.path.join(project.project_dir, next_rel_path)

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

    metadata = _extract_asset_media_metadata(next_abs_path, asset.asset_type)
    asset.width = metadata["width"]
    asset.height = metadata["height"]
    asset.frame_count = metadata["frame_count"]
    asset.fps = metadata["fps"]
    asset.duration_sec = metadata["duration_sec"]
    asset.sample_rate = metadata["sample_rate"]
    asset.has_audio = metadata["has_audio"]
    asset.artifact_kind = replacement_artifact_kind if asset.asset_type == "artifact" else ""

    old_basename = os.path.basename(old_rel_path or "")
    if not asset.name or asset.name == old_basename:
        asset.name = os.path.basename(asset.path)

    return asset


if routes is not None:

    # -----------------------------------------------------------------------
    # Project CRUD
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/projects")
    async def api_list_projects(request: web.Request) -> web.Response:
        base_dir = request.query.get("base_dir", "") or _get_base_dir()
        if not base_dir:
            return _json_error("base_dir required", 400)
        projects = list_projects(base_dir)
        return web.json_response({"projects": projects})

    @routes.get("/sonder-editor/project/{project_id}")
    async def api_get_project(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
            return web.json_response(project.to_dict())
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

    @routes.get("/sonder-editor/project/{project_id}/dormant_summary")
    async def api_get_dormant_summary(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
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
        width = body.get("width", 768)
        height = body.get("height", 512)
        template_id = body.get("template_id", "free") or "free"
        base_dir = body.get("base_dir", "") or _get_base_dir()

        if not base_dir:
            return _json_error("base_dir is required", 400)

        try:
            project = create_project(name, fps, width, height, template_id, base_dir)
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
        if "metadata" in body:
            project.metadata.update(body["metadata"])

        save_project(project)
        return web.json_response(project.to_dict())

    # -----------------------------------------------------------------------
    # Asset management
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/project/{project_id}/assets")
    async def api_list_assets(request: web.Request) -> web.Response:
        """List all assets in a project, optionally filtered by type.
        Auto-discovers untracked files in media/ folder.
        """
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        # Auto-discover untracked files in media/ folder
        # BUG-4 fix: persist newly discovered assets so IDs remain stable
        if _sync_media_folder(project):
            save_project(project)

        asset_type = request.query.get("type", "")
        include_trashed = _query_flag(request.query.get("include_trashed"))
        if asset_type:
            assets = project.get_assets_by_type(asset_type)
        else:
            assets = project.assets
        if not include_trashed:
            assets = [asset for asset in assets if not _asset_is_trashed(asset)]

        result = [_asset_payload(project, asset) for asset in assets]

        return web.json_response({"assets": result, "folders": _collect_asset_folders(project)})

    @routes.get("/sonder-editor/project/{project_id}/assets/dormant")
    async def api_list_dormant_assets(request: web.Request) -> web.Response:
        """List lightweight asset data without scanning/syncing media folders."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        include_trashed = _query_flag(request.query.get("include_trashed"))
        assets = project.assets if include_trashed else [asset for asset in project.assets if not _asset_is_trashed(asset)]
        result = [_asset_payload(project, asset) for asset in assets]

        return web.json_response({"assets": result, "folders": _collect_asset_folders(project)})

    @routes.post("/sonder-editor/project/{project_id}/assets/import")
    async def api_import_asset(request: web.Request) -> web.Response:
        """Import a media file into the project's media directory."""
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
        media_dir = os.path.join(project.project_dir, "media")
        os.makedirs(media_dir, exist_ok=True)
        basename = os.path.basename(source_path)
        dest_filename = f"{uuid.uuid4().hex[:8]}_{basename}"
        dest_path = os.path.join(media_dir, dest_filename)
        shutil.copy2(source_path, dest_path)

        metadata = _extract_asset_media_metadata(dest_path, asset_type)

        # Create asset entry
        asset = Asset(
            name=basename,
            asset_type=asset_type,
            artifact_kind=artifact_kind,
            path=os.path.join("media", dest_filename),
            width=metadata["width"],
            height=metadata["height"],
            frame_count=metadata["frame_count"],
            fps=metadata["fps"],
            duration_sec=metadata["duration_sec"],
            sample_rate=metadata["sample_rate"],
            has_audio=metadata["has_audio"],
            prompt=body.get("prompt", ""),
            generation_params=body.get("generation_params", {}),
        )
        if body.get("folder"):
            asset.folder = _normalize_asset_folder(body["folder"])
            _ensure_asset_folder(project, asset.folder)
        project.add_asset(asset)
        save_project(project)

        if asset_type in {"video", "image", "audio"}:
            thumb_path = os.path.join(
                project.project_dir, "cache", "thumbnails",
                f"{asset.asset_id}.png"
            )
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

    @routes.post("/sonder-editor/project/{project_id}/assets/extract_frame")
    async def api_extract_frame(request: web.Request) -> web.Response:
        """Extract a single video frame and save as an image asset."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        body = await request.json()
        source_path = body.get("source_path", "")
        frame_index = int(body.get("frame_index", 0))

        if not source_path:
            return _json_error("source_path is required", 400)

        # Resolve path relative to project dir
        abs_path = source_path
        if not os.path.isabs(source_path):
            abs_path = os.path.join(project.project_dir, source_path)
        if not os.path.isfile(abs_path):
            return _json_error(f"Source file not found: {source_path}", 404)

        try:
            media_dir = os.path.join(project.project_dir, "media")
            os.makedirs(media_dir, exist_ok=True)
            out_filename = f"{uuid.uuid4().hex[:8]}_frame_{frame_index}.png"
            out_path = os.path.join(media_dir, out_filename)

            # Prefer ffmpeg for frame extraction to preserve video decode fidelity.
            # Fall back to OpenCV if ffmpeg is unavailable or fails.
            extracted_size = _extract_video_frame_ffmpeg(abs_path, frame_index, out_path)
            if extracted_size is not None:
                w, h = extracted_size
            else:
                import cv2
                cap = cv2.VideoCapture(abs_path)
                if not cap.isOpened():
                    return _json_error("Could not open video file", 500)

                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
                ret, frame_bgr = cap.read()
                cap.release()

                if not ret:
                    return _json_error(f"Could not read frame {frame_index}", 500)

                h, w = frame_bgr.shape[:2]
                cv2.imwrite(out_path, frame_bgr, [cv2.IMWRITE_PNG_COMPRESSION, 0])

            asset = Asset(
                name=f"Frame {frame_index}",
                asset_type="image",
                path=os.path.join("media", out_filename),
                width=w,
                height=h,
            )
            project.add_asset(asset)
            save_project(project)

            # Generate thumbnail
            thumb_path = os.path.join(
                project.project_dir, "cache", "thumbnails",
                f"{asset.asset_id}.png"
            )
            ensure_thumbnail("image", out_path, thumb_path)

            return web.json_response(asset.to_dict(), status=201)

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

        trashed_ids = []
        for asset in assets:
            _trash_project_asset(asset)
            trashed_ids.append(asset.asset_id)
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
            for asset in assets:
                _delete_project_asset(project, asset)
                deleted_ids.append(asset.asset_id)
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
            for asset in list(_project_trashed_assets(project)):
                _delete_project_asset(project, asset)
                deleted_ids.append(asset.asset_id)
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

        save_project(project)
        return web.json_response(_asset_payload(project, asset))

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

        payload = _trash_project_asset(asset)
        save_project(project)
        return web.json_response(payload)

    @routes.post("/sonder-editor/project/{project_id}/assets/{asset_id}/replace")
    async def api_replace_asset(request: web.Request) -> web.Response:
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
        except ValueError as e:
            return _json_error(str(e), 400)

        _delete_asset_cache_files(project, asset)

        if not _asset_missing(project, asset):
            thumb_path = os.path.join(project.project_dir, "cache", "thumbnails", f"{asset.asset_id}.png")
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
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset_id = request.match_info["asset_id"]
        asset = project.get_asset(asset_id)
        if not asset:
            return _json_error(f"Asset not found: {asset_id}", 404)

        thumb_path = os.path.join(
            project.project_dir, "cache", "thumbnails",
            f"{asset_id}.png"
        )
        if os.path.isfile(thumb_path):
            return web.FileResponse(thumb_path)

        source_path = _asset_abspath(project, asset)
        if not source_path or not os.path.isfile(source_path):
            return _json_error("Thumbnail unavailable for missing asset", 404)
        if not ensure_thumbnail(asset.asset_type, source_path, thumb_path):
            return _json_error("Failed to generate thumbnail", 500)

        return web.FileResponse(thumb_path)

    @routes.get("/sonder-editor/project/{project_id}/thumbnail_strip/{asset_id}")
    async def api_get_thumbnail_strip(request: web.Request) -> web.Response:
        """Serve a filmstrip thumbnail for a video asset (tiled frames)."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset_id = request.match_info["asset_id"]
        asset = project.get_asset(asset_id)
        if not asset or asset.asset_type != "video":
            return _json_error(f"Video asset not found: {asset_id}", 404)

        strip_path = os.path.join(
            project.project_dir, "cache", "thumbnails",
            f"{asset_id}_strip.jpg"
        )
        info_path = strip_path + ".json"

        # Generate if not cached
        if not os.path.isfile(strip_path):
            source_path = _asset_abspath(project, asset)
            if not source_path or not os.path.isfile(source_path):
                return _json_error("Thumbnail strip unavailable for missing asset", 404)
            info = generate_thumbnail_strip(source_path, strip_path)
            if info:
                import json as _json
                with open(info_path, "w") as f:
                    _json.dump(info, f)
            else:
                return _json_error("Failed to generate thumbnail strip", 500)

        # Return info JSON or image
        if request.query.get("info"):
            if os.path.isfile(info_path):
                return web.FileResponse(info_path, headers={"Content-Type": "application/json"})
            return _json_error("Strip info not found", 404)

        return web.FileResponse(strip_path)

    @routes.get("/sonder-editor/project/{project_id}/waveform/{asset_id}")
    async def api_get_waveform(request: web.Request) -> web.Response:
        """Serve waveform peaks data for an audio asset or a video asset with audio."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        asset_id = request.match_info["asset_id"]
        asset = project.get_asset(asset_id)
        if not asset or asset.asset_type not in {"audio", "video"}:
            return _json_error(f"Audio-capable asset not found: {asset_id}", 404)
        if asset.asset_type == "video" and not asset.has_audio:
            return _json_error("Waveform unavailable for video without audio", 404)

        waveform_path = os.path.join(
            project.project_dir, "cache", "waveforms",
            f"{asset_id}.json"
        )

        # Generate if not cached
        if not os.path.isfile(waveform_path):
            source_path = _asset_abspath(project, asset)
            if not source_path or not os.path.isfile(source_path):
                return _json_error("Waveform unavailable for missing asset", 404)
            waveform_source = source_path
            extracted_audio_path = os.path.join(
                project.project_dir, "cache", "waveforms",
                f"{asset_id}_audio.wav"
            )
            if asset.asset_type == "video":
                if not os.path.isfile(extracted_audio_path) and not _extract_audio_from_video(source_path, extracted_audio_path):
                    return _json_error("Failed to extract video audio for waveform", 500)
                waveform_source = extracted_audio_path
            data = generate_waveform_data(waveform_source, waveform_path)
            if not data:
                return _json_error("Failed to generate waveform data", 500)

        return web.FileResponse(waveform_path, headers={"Content-Type": "application/json"})

    # -----------------------------------------------------------------------
    # Scene CRUD
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/project/{project_id}/scenes")
    async def api_list_scenes(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        return web.json_response({
            "scenes": [s.to_dict() for s in project.scenes_ordered()]
        })

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

        if "name" in body:
            scene.name = body["name"]
        if "duration_frames" in body:
            scene.duration_frames = int(body["duration_frames"])
        if "prompt" in body:
            scene.prompt = body["prompt"]
        if "prompt_sections" in body:
            scene.prompt_sections = [
                PromptSection.from_dict(p) for p in body["prompt_sections"]
            ]
        if "generation_params" in body:
            scene.generation_params = body["generation_params"]
        if "video_lane_count" in body:
            scene.video_lane_count = max(1, int(body["video_lane_count"]))
        if "audio_lane_count" in body:
            scene.audio_lane_count = max(1, int(body["audio_lane_count"]))
        if "video_lane_configs" in body:
            scene.video_lane_configs = [
                LaneConfig.from_dict(c) for c in body["video_lane_configs"]
            ]
        if "audio_lane_configs" in body:
            scene.audio_lane_configs = [
                LaneConfig.from_dict(c) for c in body["audio_lane_configs"]
            ]
        if "width" in body:
            scene.width = int(body["width"])
        if "height" in body:
            scene.height = int(body["height"])
        if "fps" in body:
            scene.fps = float(body["fps"])
        # Auto-pad configs to match lane counts
        while len(scene.video_lane_configs) < scene.video_lane_count:
            scene.video_lane_configs.append(LaneConfig())
        while len(scene.audio_lane_configs) < scene.audio_lane_count:
            scene.audio_lane_configs.append(LaneConfig())

        save_project(project)
        return web.json_response(scene.to_dict())

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
        if "audio_lane_count" in body:
            scene.audio_lane_count = max(1, int(body["audio_lane_count"]))
        if "video_lane_configs" in body:
            scene.video_lane_configs = [
                LaneConfig.from_dict(c) for c in body["video_lane_configs"]
            ]
        if "audio_lane_configs" in body:
            scene.audio_lane_configs = [
                LaneConfig.from_dict(c) for c in body["audio_lane_configs"]
            ]
        while len(scene.video_lane_configs) < scene.video_lane_count:
            scene.video_lane_configs.append(LaneConfig())
        while len(scene.audio_lane_configs) < scene.audio_lane_count:
            scene.audio_lane_configs.append(LaneConfig())

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

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        guide = GuideFrame(
            frame_index=body.get("frame_index", 0),
            asset_id=body.get("asset_id", ""),
            source=body.get("source", "asset"),
            strength=body.get("strength", 1.0),
        )

        # Replace existing guide at the same frame index
        scene.guide_frames = [
            g for g in scene.guide_frames if g.frame_index != guide.frame_index
        ]
        scene.guide_frames.append(guide)
        scene.guide_frames.sort(key=lambda g: g.frame_index)

        save_project(project)
        return web.json_response(guide.to_dict(), status=201)

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

        frame_index = int(request.match_info["frame_index"])
        original_count = len(scene.guide_frames)
        scene.guide_frames = [
            g for g in scene.guide_frames if g.frame_index != frame_index
        ]

        if len(scene.guide_frames) == original_count:
            return _json_error(f"No guide at frame {frame_index}", 404)

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

        start_frame = int(body.get("timeline_start_frame", 0))
        frame_count = asset.frame_count or 1
        end_frame = start_frame + frame_count

        clip = ClipReference(
            source_path=asset.path,
            timeline_start_frame=start_frame,
            timeline_end_frame=end_frame,
            source_in_frame=0,
            source_out_frame=frame_count,
            total_source_frames=frame_count,
            track_index=int(body.get("track_index", 0)),
        )
        scene.clips.append(clip)

        # Dual drop: also create audio track if video has audio
        # Wrapped in try/except so audio extraction failure doesn't prevent clip creation
        audio_track_dict = None
        if body.get("dual_drop") and asset.asset_type == "video":
            try:
                video_path = os.path.join(project.project_dir, asset.path)
                audio_filename = f"{asset.asset_id}_audio.wav"
                audio_rel_path = os.path.join("media", audio_filename)
                audio_abs_path = os.path.join(project.project_dir, audio_rel_path)

                # Extract audio if not already done
                if not os.path.isfile(audio_abs_path):
                    _extract_audio_from_video(video_path, audio_abs_path)

                if os.path.isfile(audio_abs_path):
                    # Find or create audio asset
                    audio_asset = next(
                        (a for a in project.assets if a.path == audio_rel_path), None
                    )
                    if not audio_asset:
                        audio_dur = _get_audio_duration(audio_abs_path)
                        audio_asset = Asset(
                            name=f"{asset.name} (audio)",
                            asset_type="audio",
                            path=audio_rel_path,
                            duration_sec=audio_dur,
                        )
                        project.add_asset(audio_asset)
                        # Generate waveform thumbnail
                        thumb_path = os.path.join(
                            project.project_dir, "cache", "thumbnails",
                            f"{audio_asset.asset_id}.png"
                        )
                        ensure_thumbnail("audio", audio_abs_path, thumb_path)

                    fps = project.fps or 24.0
                    audio_frames = int(audio_asset.duration_sec * fps) if audio_asset.duration_sec > 0 else frame_count
                    audio_lane_idx = int(body.get("audio_lane_index", 0))
                    audio_track = AudioTrack(
                        source_path=audio_asset.path,
                        timeline_start_frame=start_frame,
                        timeline_end_frame=start_frame + audio_frames,
                        total_source_frames=audio_frames,
                        lane_index=audio_lane_idx,
                    )
                    scene.audio_tracks.append(audio_track)
                    audio_track_dict = audio_track.to_dict()
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
        original_count = len(scene.clips)
        scene.clips = [c for c in scene.clips if c.clip_id != clip_id]

        if len(scene.clips) == original_count:
            return _json_error(f"Clip not found: {clip_id}", 404)

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

        if "timeline_start_frame" in body:
            new_start = int(body["timeline_start_frame"])
            if "timeline_end_frame" not in body:
                # Move: preserve duration
                duration = clip.timeline_end_frame - clip.timeline_start_frame
                clip.timeline_start_frame = max(0, new_start)
                clip.timeline_end_frame = clip.timeline_start_frame + duration
            else:
                clip.timeline_start_frame = max(0, new_start)

        if "timeline_end_frame" in body:
            clip.timeline_end_frame = int(body["timeline_end_frame"])
        if "source_in_frame" in body:
            clip.source_in_frame = int(body["source_in_frame"])
        if "source_out_frame" in body:
            clip.source_out_frame = int(body["source_out_frame"])
        if "opacity" in body:
            clip.opacity = float(body["opacity"])
        if "track_index" in body:
            clip.track_index = int(body["track_index"])

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
        if split_frame <= clip.timeline_start_frame or split_frame >= clip.timeline_end_frame:
            return _json_error("Split frame must be within clip range", 400)

        # Calculate source frame offset at the split point
        source_offset = split_frame - clip.timeline_start_frame
        source_split = (clip.source_in_frame or 0) + source_offset

        orig_source_out = clip.source_out_frame or clip.timeline_end_frame - clip.timeline_start_frame

        left_source_in = clip.source_in_frame or 0

        # Create second clip (right half) — each piece is its own complete unit
        clip2 = ClipReference(
            source_path=clip.source_path,
            timeline_start_frame=split_frame,
            timeline_end_frame=clip.timeline_end_frame,
            source_in_frame=source_split,
            source_out_frame=orig_source_out,
            total_source_frames=orig_source_out - source_split,  # own range = no ghost
            source_origin_frame=source_split,                    # origin = source_in at split
            track_index=clip.track_index,
        )

        # Trim first clip (left half)
        clip.timeline_end_frame = split_frame
        clip.source_out_frame = source_split
        clip.total_source_frames = source_split - left_source_in  # own range = no ghost
        clip.source_origin_frame = left_source_in                 # origin = source_in at split

        scene.clips.append(clip2)
        save_project(project)

        return web.json_response({
            "left": clip.to_dict(),
            "right": clip2.to_dict(),
        })

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

        start_frame = int(body.get("timeline_start_frame", 0))
        # Calculate duration in frames from asset duration
        fps = project.fps or 24.0
        duration_frames = int(asset.duration_sec * fps) if asset.duration_sec > 0 else 1
        end_frame = start_frame + duration_frames

        track = AudioTrack(
            source_path=asset.path,
            timeline_start_frame=start_frame,
            timeline_end_frame=end_frame,
            total_source_frames=duration_frames,
            lane_index=int(body.get("lane_index", 0)),
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
        original_count = len(scene.audio_tracks)
        scene.audio_tracks = [t for t in scene.audio_tracks if t.track_id != track_id]

        if len(scene.audio_tracks) == original_count:
            return _json_error(f"Audio track not found: {track_id}", 404)

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
        if split_frame <= track.timeline_start_frame or split_frame >= track.timeline_end_frame:
            return _json_error("Split frame must be within track range", 400)

        # Calculate source offset at the split point
        source_offset = split_frame - track.timeline_start_frame
        source_split = (track.source_in_frame or 0) + source_offset

        orig_end_frame = track.timeline_end_frame
        right_duration = orig_end_frame - split_frame
        left_duration = split_frame - track.timeline_start_frame

        left_source_in = track.source_in_frame or 0

        # Create second track (right half) — each piece is its own complete unit
        track2 = AudioTrack(
            source_path=track.source_path,
            timeline_start_frame=split_frame,
            timeline_end_frame=orig_end_frame,
            source_in_frame=source_split,
            total_source_frames=right_duration,   # own range = no ghost
            source_origin_frame=source_split,     # origin = source_in at split
            volume=track.volume,
            muted=track.muted,
            lane_index=track.lane_index,          # preserve lane on split
        )

        # Trim first track (left half)
        track.timeline_end_frame = split_frame
        track.total_source_frames = left_duration     # own range = no ghost
        track.source_origin_frame = left_source_in    # origin = source_in at split

        scene.audio_tracks.append(track2)
        save_project(project)

        return web.json_response({
            "left": track.to_dict(),
            "right": track2.to_dict(),
        })

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

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        section = PromptSection(
            start_frame=int(body.get("start_frame", 0)),
            end_frame=int(body.get("end_frame", 0)),
            prompt=body.get("prompt", ""),
        )
        scene.prompt_sections.append(section)
        scene.prompt_sections.sort(key=lambda s: s.start_frame)
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

        idx = int(request.match_info["index"])
        if idx < 0 or idx >= len(scene.prompt_sections):
            return _json_error(f"Prompt section index out of range: {idx}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        section = scene.prompt_sections[idx]
        if "start_frame" in body:
            section.start_frame = int(body["start_frame"])
        if "end_frame" in body:
            section.end_frame = int(body["end_frame"])
        if "prompt" in body:
            section.prompt = body["prompt"]

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

        idx = int(request.match_info["index"])
        if idx < 0 or idx >= len(scene.prompt_sections):
            return _json_error(f"Prompt section index out of range: {idx}", 404)

        scene.prompt_sections.pop(idx)
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
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        return web.json_response([j.to_dict() for j in project.generation_queue])

    @routes.post("/sonder-editor/project/{project_id}/queue")
    async def api_add_queue_job(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        raw_params = body.get("params", {}) or {}
        params = dict(raw_params) if isinstance(raw_params, dict) else {}
        if any(field in body for field in (
            "pre_context_frames",
            "post_context_frames",
            "guide_frame_snapshots",
            "prompt_sections",
            "scene_width",
            "scene_height",
            "scene_fps",
            "template_id",
        )):
            params["snapshot_version"] = 1

        job = GenerationJob(
            scene_id=body.get("scene_id", ""),
            scene_name=body.get("scene_name", ""),
            selection_start=int(body.get("selection_start", 0)),
            selection_end=int(body.get("selection_end", 0)),
            batch_id=str(body.get("batch_id", "") or ""),
            batch_total=int(body.get("batch_total", 0)),
            batch_index=int(body.get("batch_index", 0)),
            prompt=body.get("prompt", ""),
            context_frames=int(body.get("context_frames", 0)),
            pre_context_frames=int(body.get("pre_context_frames", 0)),
            post_context_frames=int(body.get("post_context_frames", 0)),
            guide_frame_snapshots=list(body.get("guide_frame_snapshots", []) or []),
            prompt_sections=list(body.get("prompt_sections", []) or []),
            scene_width=int(body.get("scene_width", 0)),
            scene_height=int(body.get("scene_height", 0)),
            scene_fps=float(body.get("scene_fps", 0.0) or 0.0),
            template_id=str(body.get("template_id", "free") or "free"),
            params=params,
        )
        project.generation_queue.append(job)
        save_project(project)
        return web.json_response(job.to_dict())

    @routes.put("/sonder-editor/project/{project_id}/queue/{job_id}")
    async def api_update_queue_job(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        job_id = request.match_info["job_id"]
        job = next((j for j in project.generation_queue if j.job_id == job_id), None)
        if not job:
            return _json_error(f"Job not found: {job_id}", 404)

        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        if "status" in body:
            job.status = body["status"]
        if "progress" in body:
            job.progress = float(body["progress"])
        if "error" in body:
            job.error = body["error"]
        if "result_asset_id" in body:
            job.result_asset_id = body["result_asset_id"]
        if "completed_at" in body:
            job.completed_at = body["completed_at"]

        save_project(project)
        return web.json_response(job.to_dict())

    @routes.delete("/sonder-editor/project/{project_id}/queue/{job_id}")
    async def api_delete_queue_job(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        job_id = request.match_info["job_id"]
        before = len(project.generation_queue)
        project.generation_queue = [j for j in project.generation_queue if j.job_id != job_id]
        if len(project.generation_queue) == before:
            return _json_error(f"Job not found: {job_id}", 404)

        save_project(project)
        return web.json_response({"status": "deleted"})

    @routes.delete("/sonder-editor/project/{project_id}/queue")
    async def api_clear_queue(request: web.Request) -> web.Response:
        """Clear completed/failed jobs, or all if ?all=1."""
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        if request.query.get("all") == "1":
            project.generation_queue.clear()
        else:
            # Default: clear completed and failed only
            project.generation_queue = [
                j for j in project.generation_queue
                if j.status not in ("completed", "failed")
            ]

        save_project(project)
        return web.json_response({"status": "cleared"})

    # -----------------------------------------------------------------------
    # WebSocket stub
    # -----------------------------------------------------------------------

    @routes.get("/sonder-editor/ws")
    async def api_websocket(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    await ws.send_json({"type": "ack", "received": data.get("type", "unknown")})
                except json.JSONDecodeError:
                    await ws.send_json({"type": "error", "message": "Invalid JSON"})
            elif msg.type == web.WSMsgType.ERROR:
                logger.error("WebSocket error: %s", ws.exception())

        return ws
