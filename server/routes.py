import json
import logging
import os
import shutil
import uuid

from aiohttp import web

from .project_manager import create_project, load_project, save_project, list_projects
from .timeline_state import (
    TimelineProject, Asset, Scene, GuideFrame, PromptSection, AudioTrack,
    ClipReference,
)
from .thumbnail_service import ensure_thumbnail

logger = logging.getLogger("ltx_editor")

# Defer route registration until ComfyUI's PromptServer is available.
try:
    from server import PromptServer
    routes = PromptServer.instance.routes
except Exception:
    routes = None
    logger.warning("PromptServer not available — LTX Editor API routes disabled")


def _json_error(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tiff"}
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a"}


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


def _sync_media_folder(project: TimelineProject) -> None:
    """Scan media/ folder for files not yet in the asset registry and add them."""
    media_dir = os.path.join(project.project_dir, "media")
    if not os.path.isdir(media_dir):
        return

    # Build set of known relative paths
    known_paths = {a.path for a in project.assets}

    changed = False
    for filename in os.listdir(media_dir):
        filepath = os.path.join(media_dir, filename)
        if not os.path.isfile(filepath):
            continue

        rel_path = os.path.join("media", filename)
        if rel_path in known_paths:
            continue

        # Determine type from extension
        ext = os.path.splitext(filename)[1].lower()
        if ext in VIDEO_EXTS:
            asset_type = "video"
        elif ext in IMAGE_EXTS:
            asset_type = "image"
        elif ext in AUDIO_EXTS:
            asset_type = "audio"
        else:
            continue  # skip unknown file types

        # Extract metadata
        width, height, frame_count, fps, duration_sec, sample_rate = 0, 0, 0, 0.0, 0.0, 0
        if asset_type == "video":
            try:
                import cv2
                cap = cv2.VideoCapture(filepath)
                if cap.isOpened():
                    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                    fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
                    duration_sec = frame_count / fps if fps > 0 else 0.0
                    cap.release()
            except Exception:
                pass
        elif asset_type == "image":
            try:
                from PIL import Image as PILImage
                img = PILImage.open(filepath)
                width, height = img.size
            except Exception:
                pass
        elif asset_type == "audio":
            duration_sec = _get_audio_duration(filepath)

        asset = Asset(
            name=filename,
            asset_type=asset_type,
            path=rel_path,
            width=width,
            height=height,
            frame_count=frame_count,
            fps=fps,
            duration_sec=duration_sec,
            sample_rate=sample_rate,
        )
        project.add_asset(asset)

        # Generate thumbnail
        thumb_path = os.path.join(
            project.project_dir, "cache", "thumbnails",
            f"{asset.asset_id}.png"
        )
        ensure_thumbnail(asset_type, filepath, thumb_path)

        changed = True
        logger.info("Auto-registered asset: %s (%s)", filename, asset_type)

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



def _get_base_dir() -> str:
    """Get the default base directory for projects."""
    try:
        import folder_paths
        return os.path.join(folder_paths.get_output_directory(), "ltx_projects")
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


if routes is not None:

    # -----------------------------------------------------------------------
    # Project CRUD
    # -----------------------------------------------------------------------

    @routes.get("/ltx-editor/projects")
    async def api_list_projects(request: web.Request) -> web.Response:
        base_dir = request.query.get("base_dir", "") or _get_base_dir()
        if not base_dir:
            return _json_error("base_dir required", 400)
        projects = list_projects(base_dir)
        return web.json_response({"projects": projects})

    @routes.get("/ltx-editor/project/{project_id}")
    async def api_get_project(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
            return web.json_response(project.to_dict())
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

    @routes.post("/ltx-editor/project")
    async def api_create_project(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _json_error("Invalid JSON body", 400)

        name = body.get("name", "Untitled")
        fps = body.get("fps", 24.0)
        width = body.get("width", 768)
        height = body.get("height", 512)
        base_dir = body.get("base_dir", "") or _get_base_dir()

        if not base_dir:
            return _json_error("base_dir is required", 400)

        try:
            project = create_project(name, fps, width, height, base_dir)
            return web.json_response(project.to_dict(), status=201)
        except Exception as e:
            logger.exception("Failed to create project")
            return _json_error(str(e), 500)

    @routes.put("/ltx-editor/project/{project_id}")
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
        if "metadata" in body:
            project.metadata.update(body["metadata"])

        save_project(project)
        return web.json_response(project.to_dict())

    # -----------------------------------------------------------------------
    # Asset management
    # -----------------------------------------------------------------------

    @routes.get("/ltx-editor/project/{project_id}/assets")
    async def api_list_assets(request: web.Request) -> web.Response:
        """List all assets in a project, optionally filtered by type.
        Auto-discovers untracked files in media/ folder.
        """
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        # Auto-discover untracked files in media/ folder
        _sync_media_folder(project)

        asset_type = request.query.get("type", "")
        if asset_type:
            assets = project.get_assets_by_type(asset_type)
        else:
            assets = project.assets

        result = []
        for asset in assets:
            d = asset.to_dict()
            thumb_path = os.path.join(
                project.project_dir, "cache", "thumbnails",
                f"{asset.asset_id}.png"
            )
            d["has_thumbnail"] = os.path.isfile(thumb_path)
            result.append(d)

        return web.json_response({"assets": result})

    @routes.post("/ltx-editor/project/{project_id}/assets/import")
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

        source_path = body.get("source_path", "")

        # Resolve relative paths against ComfyUI's input directory
        if source_path and not os.path.isfile(source_path):
            try:
                import folder_paths
                input_path = os.path.join(folder_paths.get_input_directory(), source_path)
                if os.path.isfile(input_path):
                    source_path = input_path
            except Exception:
                pass

        if not source_path or not os.path.isfile(source_path):
            return _json_error(f"File not found: {source_path}", 400)

        # Determine asset type from extension
        ext = os.path.splitext(source_path)[1].lower()

        if ext in VIDEO_EXTS:
            asset_type = "video"
        elif ext in IMAGE_EXTS:
            asset_type = "image"
        elif ext in AUDIO_EXTS:
            asset_type = "audio"
        else:
            asset_type = body.get("type", "video")

        # Copy to project media directory
        media_dir = os.path.join(project.project_dir, "media")
        os.makedirs(media_dir, exist_ok=True)
        basename = os.path.basename(source_path)
        dest_filename = f"{uuid.uuid4().hex[:8]}_{basename}"
        dest_path = os.path.join(media_dir, dest_filename)
        shutil.copy2(source_path, dest_path)

        # Extract metadata
        width, height, frame_count, fps, duration_sec, sample_rate = 0, 0, 0, 0.0, 0.0, 0
        if asset_type == "video":
            try:
                import cv2
                cap = cv2.VideoCapture(dest_path)
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
                img = Image.open(dest_path)
                width, height = img.size
            except Exception as e:
                logger.warning("Failed to extract image metadata: %s", e)
        elif asset_type == "audio":
            duration_sec = _get_audio_duration(dest_path)

        # Create asset entry
        asset = Asset(
            name=basename,
            asset_type=asset_type,
            path=os.path.join("media", dest_filename),
            width=width,
            height=height,
            frame_count=frame_count,
            fps=fps,
            duration_sec=duration_sec,
            sample_rate=sample_rate,
            prompt=body.get("prompt", ""),
            generation_params=body.get("generation_params", {}),
        )
        project.add_asset(asset)
        save_project(project)

        # Generate thumbnail
        thumb_path = os.path.join(
            project.project_dir, "cache", "thumbnails",
            f"{asset.asset_id}.png"
        )
        ensure_thumbnail(asset_type, dest_path, thumb_path)

        return web.json_response(asset.to_dict(), status=201)

    @routes.get("/ltx-editor/project/{project_id}/thumbnail/{asset_id}")
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
        source_path = os.path.join(project.project_dir, asset.path)

        if not ensure_thumbnail(asset.asset_type, source_path, thumb_path):
            return _json_error("Failed to generate thumbnail", 500)

        return web.FileResponse(thumb_path)

    # -----------------------------------------------------------------------
    # Scene CRUD
    # -----------------------------------------------------------------------

    @routes.get("/ltx-editor/project/{project_id}/scenes")
    async def api_list_scenes(request: web.Request) -> web.Response:
        try:
            project = _load_project_from_request(request)
        except FileNotFoundError as e:
            return _json_error(str(e), 404)

        return web.json_response({
            "scenes": [s.to_dict() for s in project.scenes_ordered()]
        })

    @routes.post("/ltx-editor/project/{project_id}/scenes")
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

    @routes.get("/ltx-editor/project/{project_id}/scenes/{scene_id}")
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

    @routes.put("/ltx-editor/project/{project_id}/scenes/{scene_id}")
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

        save_project(project)
        return web.json_response(scene.to_dict())

    @routes.delete("/ltx-editor/project/{project_id}/scenes/{scene_id}")
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

    @routes.put("/ltx-editor/project/{project_id}/scenes/{scene_id}/restore")
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

        save_project(project)
        return web.json_response(scene.to_dict())

    # -----------------------------------------------------------------------
    # Guide frames
    # -----------------------------------------------------------------------

    @routes.post("/ltx-editor/project/{project_id}/scenes/{scene_id}/guides")
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

    @routes.delete("/ltx-editor/project/{project_id}/scenes/{scene_id}/guides/{frame_index}")
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

    @routes.post("/ltx-editor/project/{project_id}/scenes/{scene_id}/clips")
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
        save_project(project)

        return web.json_response(clip.to_dict(), status=201)

    @routes.delete("/ltx-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}")
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

    @routes.put("/ltx-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}")
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

        save_project(project)
        return web.json_response(clip.to_dict())

    @routes.post("/ltx-editor/project/{project_id}/scenes/{scene_id}/clips/{clip_id}/split")
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

    @routes.post("/ltx-editor/project/{project_id}/scenes/{scene_id}/audio_tracks")
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
        )
        scene.audio_tracks.append(track)
        save_project(project)

        return web.json_response(track.to_dict(), status=201)

    @routes.delete("/ltx-editor/project/{project_id}/scenes/{scene_id}/audio_tracks/{track_id}")
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

    @routes.put("/ltx-editor/project/{project_id}/scenes/{scene_id}/audio_tracks/{track_id}")
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

        save_project(project)
        return web.json_response(track.to_dict())

    @routes.post("/ltx-editor/project/{project_id}/scenes/{scene_id}/audio_tracks/{track_id}/split")
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

    @routes.post("/ltx-editor/project/{project_id}/scenes/{scene_id}/prompt_sections")
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

    @routes.put("/ltx-editor/project/{project_id}/scenes/{scene_id}/prompt_sections/{index}")
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

    @routes.delete("/ltx-editor/project/{project_id}/scenes/{scene_id}/prompt_sections/{index}")
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
    # WebSocket stub
    # -----------------------------------------------------------------------

    @routes.get("/ltx-editor/ws")
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
