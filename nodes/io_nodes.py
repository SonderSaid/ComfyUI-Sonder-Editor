import os
import uuid
import random
import logging
import subprocess
from datetime import datetime

import cv2
import numpy as np
import torch
import folder_paths

from PIL import Image

from ..server.timeline_state import ClipReference, Asset, LaneConfig
from ..server.project_manager import save_project

logger = logging.getLogger("ltx_editor")


def _get_ffmpeg() -> str:
    """Return ffmpeg path, preferring imageio_ffmpeg if available."""
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "ffmpeg"


def _frames_to_tensor(frames: list[np.ndarray]) -> torch.Tensor:
    """Convert list of BGR uint8 frames to (N, H, W, 3) float32 RGB tensor."""
    rgb_frames = [cv2.cvtColor(f, cv2.COLOR_BGR2RGB) for f in frames]
    arr = np.stack(rgb_frames, axis=0).astype(np.float32) / 255.0
    return torch.from_numpy(arr)


def _tensor_to_frames(tensor: torch.Tensor) -> list[np.ndarray]:
    """Convert (N, H, W, 3) float32 RGB tensor to list of BGR uint8 frames."""
    arr = (tensor.cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
    return [cv2.cvtColor(arr[i], cv2.COLOR_RGB2BGR) for i in range(arr.shape[0])]


def _save_preview_thumbnail(frame_bgr: np.ndarray, prefix: str = "ltx_thumb") -> list[dict]:
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


class LTXSaveVideo:
    """Save IMAGE frames as a video file with optional audio."""

    CATEGORY = "LTX-Editor/IO"
    OUTPUT_NODE = True
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("output_path",)
    OUTPUT_TOOLTIPS = ("Absolute path to the saved video file.",)
    FUNCTION = "save_video"
    DESCRIPTION = "Encodes an IMAGE tensor to an MP4 video file in the project's exports/ folder. Optionally muxes audio. Shows a thumbnail preview of the first frame."

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "project": ("LTX_PROJECT", {"tooltip": "The project — video saves to its exports/ folder."}),
                "frames": ("IMAGE", {"tooltip": "Batch of frames to encode as video."}),
                "filename_prefix": ("STRING", {"default": "output", "tooltip": "Prefix for the output filename."}),
                "fps": ("FLOAT", {"default": 24.0, "min": 1.0, "max": 120.0, "step": 0.001, "tooltip": "Output video frame rate."}),
                "mode": (["Video", "Take"], {"default": "Video", "tooltip": "Video: normal save. Take: saves and auto-places result on timeline as a new lane."}),
            },
            "optional": {
                "audio": ("AUDIO", {"tooltip": "Audio to mux into the video."}),
                "codec": (["libx264", "libx265"], {"tooltip": "Video codec. H.264 is most compatible, H.265 is smaller."}),
                "quality": ("INT", {"default": 23, "min": 0, "max": 51, "tooltip": "CRF quality (lower = better quality, larger file). 23 is a good default."}),
            },
        }

    def save_video(self, project, frames, filename_prefix="output", fps=24.0,
                   mode="Video", audio=None, codec="libx264", quality=23):
        # Save to media/ so it appears in the project's asset gallery
        media_dir = os.path.join(project.project_dir, "media")
        os.makedirs(media_dir, exist_ok=True)

        output_filename = f"{filename_prefix}_{uuid.uuid4().hex[:6]}.mp4"
        output_path = os.path.join(media_dir, output_filename)

        bgr_frames = _tensor_to_frames(frames)
        h, w = bgr_frames[0].shape[:2]

        ffmpeg = _get_ffmpeg()

        # Build ffmpeg command
        cmd = [
            ffmpeg,
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{w}x{h}", "-r", str(fps),
            "-i", "pipe:0",
        ]

        audio_tmp = None
        if audio is not None:
            audio_tmp = os.path.join(media_dir, f"_tmp_audio_{uuid.uuid4().hex[:6]}.wav")
            try:
                import torchaudio
                waveform = audio["waveform"]
                if waveform.dim() == 3:
                    waveform = waveform.squeeze(0)
                torchaudio.save(audio_tmp, waveform, audio["sample_rate"])
                cmd += ["-i", audio_tmp]
            except Exception as e:
                logger.warning("Failed to save temp audio: %s", e)
                audio_tmp = None

        cmd += [
            "-c:v", codec, "-crf", str(quality),
            "-pix_fmt", "yuv420p",
        ]
        if audio_tmp:
            cmd += ["-shortest"]
        cmd += [output_path, "-y"]

        raw_bytes = b"".join(f.tobytes() for f in bgr_frames)
        proc = subprocess.run(cmd, input=raw_bytes, capture_output=True, timeout=300)

        if audio_tmp and os.path.isfile(audio_tmp):
            os.remove(audio_tmp)

        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:500]}")

        # Auto-register as a project asset
        total_frames = len(bgr_frames)
        asset = Asset(
            name=output_filename,
            asset_type="video",
            path=os.path.join("media", output_filename),
            width=w,
            height=h,
            frame_count=total_frames,
            fps=fps,
            duration_sec=total_frames / fps if fps > 0 else 0.0,
        )
        project.add_asset(asset)

        # Generate thumbnail
        from ..server.thumbnail_service import ensure_thumbnail
        thumb_path = os.path.join(
            project.project_dir, "cache", "thumbnails",
            f"{asset.asset_id}.png"
        )
        ensure_thumbnail("video", output_path, thumb_path)

        # --- Take mode: auto-place on timeline ---
        if mode == "Take" and hasattr(project, '_execution_context') and project._execution_context:
            ctx = project._execution_context
            scene = project.get_scene(ctx.get("scene_id", ""))
            if scene:
                # Organize asset into Takes folder
                asset.folder = f"Takes/{ctx.get('scene_name', scene.name)}"
                asset.generation_params = dict(ctx)

                # Find next available video lane
                existing_lanes = [c.track_index for c in scene.clips] if scene.clips else [-1]
                new_lane = max(existing_lanes) + 1

                # Ensure scene has enough lanes
                if scene.video_lane_count <= new_lane:
                    scene.video_lane_count = new_lane + 1
                while len(scene.video_lane_configs) < scene.video_lane_count:
                    scene.video_lane_configs.append(LaneConfig())

                # Determine clip placement — at original selection, not context-expanded range
                sel_start = ctx.get("selection_start", 0)
                sel_end = ctx.get("selection_end", sel_start + total_frames)
                pre_ctx = ctx.get("pre_context_frames", ctx.get("context_frames", 0))
                post_ctx = ctx.get("post_context_frames", ctx.get("context_frames", 0))

                # Source trim: if context frames exist, the video has pre+generation+post
                # The clip should show only the generated portion
                source_in = pre_ctx if pre_ctx > 0 else 0
                source_out = total_frames - post_ctx if post_ctx > 0 else total_frames

                clip = ClipReference(
                    source_path=os.path.join("media", output_filename),
                    timeline_start_frame=sel_start,
                    timeline_end_frame=sel_end,
                    source_in_frame=source_in,
                    source_out_frame=source_out,
                    total_source_frames=total_frames,
                    source_origin_frame=source_in,
                    track_index=new_lane,
                    is_generated=True,
                    generation_params=dict(ctx),
                    take_metadata=dict(ctx),
                )
                scene.clips.append(clip)
                logger.info("Take auto-placed on lane %d at frames %d-%d", new_lane, sel_start, sel_end)
            else:
                logger.warning("Take mode: scene_id '%s' not found, skipping auto-placement", ctx.get("scene_id", ""))

        if hasattr(project, "_execution_context") and project._execution_context:
            ctx = project._execution_context
            queue_job_id = ctx.get("queue_job_id", "")
            if queue_job_id:
                queue_job = next((job for job in project.generation_queue if job.job_id == queue_job_id), None)
                if queue_job:
                    queue_job.status = "completed"
                    queue_job.progress = 1.0
                    queue_job.error = ""
                    queue_job.completed_at = datetime.now().isoformat()
                    queue_job.result_asset_id = asset.asset_id

        save_project(project)

        # Generate preview thumbnail for ComfyUI node display
        preview_images = _save_preview_thumbnail(bgr_frames[0], "ltx_savevid")

        logger.info("Saved video to %s (%d frames, %.1f fps)", output_path, len(bgr_frames), fps)
        return {
            "ui": {"images": preview_images},
            "result": (output_path,),
        }


class LTXPreviewVideo:
    """Preview video frames directly in ComfyUI's built-in viewer."""

    CATEGORY = "LTX-Editor/IO"
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
        }

    def preview(self, frames, fps=24.0):
        temp_dir = folder_paths.get_temp_directory()
        os.makedirs(temp_dir, exist_ok=True)

        preview_filename = f"ltx_preview_{uuid.uuid4().hex[:8]}.mp4"
        preview_path = os.path.join(temp_dir, preview_filename)

        bgr_frames = _tensor_to_frames(frames)
        h, w = bgr_frames[0].shape[:2]

        ffmpeg = _get_ffmpeg()

        cmd = [
            ffmpeg,
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{w}x{h}", "-r", str(fps),
            "-i", "pipe:0",
            "-c:v", "libx264", "-crf", "23",
            "-pix_fmt", "yuv420p",
            preview_path, "-y",
        ]

        raw_bytes = b"".join(f.tobytes() for f in bgr_frames)
        proc = subprocess.run(cmd, input=raw_bytes, capture_output=True, timeout=120)

        if proc.returncode != 0:
            logger.warning("Preview encode failed: %s", proc.stderr.decode(errors="replace")[:300])

        # Also show thumbnail on the node
        preview_images = _save_preview_thumbnail(bgr_frames[0], "ltx_preview")

        return {"ui": {
            "videos": [{"filename": preview_filename, "subfolder": "", "type": "temp"}],
            "images": preview_images,
        }}
