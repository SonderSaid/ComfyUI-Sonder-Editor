import os
import uuid
import random
import shutil
import logging
import subprocess

import cv2
import numpy as np
import torch
import folder_paths

from PIL import Image

from ..server.timeline_state import ClipReference, AudioTrack, Asset
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


def _extract_audio(video_path: str, output_path: str) -> bool:
    """Extract audio from video using ffmpeg. Returns True on success."""
    ffmpeg = _get_ffmpeg()
    try:
        result = subprocess.run(
            [ffmpeg, "-i", video_path, "-vn", "-acodec", "pcm_s16le",
             "-ar", "44100", "-ac", "2", output_path, "-y"],
            capture_output=True, timeout=120,
        )
        return result.returncode == 0 and os.path.isfile(output_path)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Audio extraction failed: %s", e)
        return False


def _load_audio_file(audio_path: str) -> dict | None:
    """Load audio file and return ComfyUI AUDIO dict {waveform, sample_rate}."""
    try:
        import torchaudio
        waveform, sample_rate = torchaudio.load(audio_path)
        # ComfyUI AUDIO format: waveform is (batch, channels, samples)
        if waveform.dim() == 2:
            waveform = waveform.unsqueeze(0)
        return {"waveform": waveform, "sample_rate": sample_rate}
    except Exception as e:
        logger.warning("Failed to load audio from %s: %s", audio_path, e)
        return None


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


def _get_input_videos():
    """List video files from ComfyUI's input directory."""
    input_dir = folder_paths.get_input_directory()
    if not os.path.isdir(input_dir):
        return []
    files = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))]
    return sorted(folder_paths.filter_files_content_types(files, ["video", "image"]))


def _get_input_audio():
    """List audio files from ComfyUI's input directory."""
    input_dir = folder_paths.get_input_directory()
    if not os.path.isdir(input_dir):
        return []
    files = [f for f in os.listdir(input_dir) if os.path.isfile(os.path.join(input_dir, f))]
    return sorted(folder_paths.filter_files_content_types(files, ["audio"]))


class LTXLoadVideo:
    """Load a video file into the project and output frames as an IMAGE tensor."""

    CATEGORY = "LTX-Editor/IO"
    RETURN_TYPES = ("IMAGE", "AUDIO", "LTX_PROJECT", "INT", "FLOAT")
    RETURN_NAMES = ("frames", "audio", "project", "frame_count", "source_fps")
    OUTPUT_TOOLTIPS = (
        "Video frames as an IMAGE batch tensor (N, H, W, 3).",
        "Audio extracted from the video (may be None if no audio track).",
        "Updated project with the new clip registered.",
        "Number of frames loaded.",
        "Original FPS of the source video.",
    )
    OUTPUT_NODE = True
    FUNCTION = "load_video"
    DESCRIPTION = "Loads a video file, extracts frames and audio, registers it as a clip in the project, and shows a thumbnail preview."

    @classmethod
    def INPUT_TYPES(s):
        video_files = _get_input_videos()
        if not video_files:
            video_files = ["(no files found)"]
        return {
            "required": {
                "project": ("LTX_PROJECT", {"tooltip": "The project to add the video to."}),
                "video": (video_files, {"tooltip": "Select a video from ComfyUI's input directory."}),
            },
            "optional": {
                "start_frame": ("INT", {"default": 0, "min": 0, "max": 999999, "tooltip": "First frame to load (0-based). Use to skip intro frames."}),
                "max_frames": ("INT", {"default": 0, "min": 0, "max": 999999, "tooltip": "Maximum frames to load. 0 = load all frames."}),
                "register_clip": ("BOOLEAN", {"default": True, "tooltip": "Add this video as a clip to the project timeline."}),
            },
        }

    @classmethod
    def IS_CHANGED(s, **kwargs):
        return float("nan")

    def load_video(self, project, video, start_frame=0, max_frames=0, register_clip=True):
        # Resolve dropdown selection to full path
        video_path = folder_paths.get_annotated_filepath(video)
        if not os.path.isfile(video_path):
            raise FileNotFoundError(f"Video file not found: {video_path}")

        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            raise RuntimeError(f"Failed to open video: {video_path}")

        try:
            source_fps = cap.get(cv2.CAP_PROP_FPS) or 24.0
            total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

            if start_frame > 0:
                cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

            frames = []
            limit = max_frames if max_frames > 0 else (total_frames - start_frame)
            for _ in range(limit):
                ret, frame = cap.read()
                if not ret:
                    break
                frames.append(frame)
        finally:
            cap.release()

        if not frames:
            raise RuntimeError(f"No frames read from {video_path}")

        frame_tensor = _frames_to_tensor(frames)

        # Generate thumbnail from first frame
        preview_images = _save_preview_thumbnail(frames[0], "ltx_loadvid")

        # Extract audio
        audio = None
        audio_path = os.path.join(
            project.project_dir, "cache",
            f"audio_{uuid.uuid4().hex[:8]}.wav"
        )
        if _extract_audio(video_path, audio_path):
            audio = _load_audio_file(audio_path)

        # Copy source to project media dir and register clip + asset
        if register_clip:
            media_dir = os.path.join(project.project_dir, "media")
            basename = os.path.basename(video_path)
            ext = os.path.splitext(basename)[1]
            dest_filename = f"{uuid.uuid4().hex[:8]}_{basename}"
            dest_path = os.path.join(media_dir, dest_filename)
            shutil.copy2(video_path, dest_path)

            # Register as a project asset with metadata
            h_vid, w_vid = frames[0].shape[:2]
            asset = Asset(
                name=basename,
                asset_type="video",
                path=os.path.join("media", dest_filename),
                width=w_vid,
                height=h_vid,
                frame_count=total_frames,
                fps=source_fps,
                duration_sec=total_frames / source_fps if source_fps > 0 else 0.0,
            )
            project.add_asset(asset)

            timeline_start = project.total_frames
            clip = ClipReference(
                source_path=dest_path,
                timeline_start_frame=timeline_start,
                timeline_end_frame=timeline_start + len(frames),
                source_in_frame=start_frame,
                source_out_frame=start_frame + len(frames),
            )
            project.add_clip(clip)
            save_project(project)

        return {
            "ui": {"images": preview_images},
            "result": (frame_tensor, audio, project, len(frames), source_fps),
        }


class LTXLoadAudio:
    """Load an audio file into the project."""

    CATEGORY = "LTX-Editor/IO"
    RETURN_TYPES = ("AUDIO", "LTX_PROJECT")
    RETURN_NAMES = ("audio", "project")
    OUTPUT_TOOLTIPS = (
        "The loaded audio waveform.",
        "Updated project with the audio track registered.",
    )
    FUNCTION = "load_audio"
    DESCRIPTION = "Loads an audio file (wav, mp3, flac, etc.) and optionally registers it as a track in the project."

    @classmethod
    def INPUT_TYPES(s):
        audio_files = _get_input_audio()
        if not audio_files:
            audio_files = ["(no files found)"]
        return {
            "required": {
                "project": ("LTX_PROJECT", {"tooltip": "The project to add the audio to."}),
                "audio": (audio_files, {"tooltip": "Select an audio file from ComfyUI's input directory."}),
            },
            "optional": {
                "register_track": ("BOOLEAN", {"default": True, "tooltip": "Add this audio as a track to the project timeline."}),
            },
        }

    @classmethod
    def IS_CHANGED(s, **kwargs):
        return float("nan")

    def load_audio(self, project, audio, register_track=True):
        audio_path = folder_paths.get_annotated_filepath(audio)
        if not os.path.isfile(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        audio_data = _load_audio_file(audio_path)
        if audio_data is None:
            raise RuntimeError(f"Failed to load audio: {audio_path}")

        if register_track:
            media_dir = os.path.join(project.project_dir, "media")
            basename = os.path.basename(audio_path)
            ext = os.path.splitext(basename)[1]
            dest_filename = f"{uuid.uuid4().hex[:8]}_{basename}"
            dest_path = os.path.join(media_dir, dest_filename)
            shutil.copy2(audio_path, dest_path)

            sample_count = audio_data["waveform"].shape[-1]
            duration_sec = sample_count / audio_data["sample_rate"]
            duration_frames = int(duration_sec * project.fps)

            # Register as a project asset
            asset = Asset(
                name=basename,
                asset_type="audio",
                path=os.path.join("media", dest_filename),
                duration_sec=duration_sec,
                sample_rate=audio_data["sample_rate"],
            )
            project.add_asset(asset)

            track = AudioTrack(
                source_path=dest_path,
                timeline_start_frame=0,
                timeline_end_frame=duration_frames,
            )
            project.add_audio_track(track)
            save_project(project)

        return (audio_data, project)


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
            },
            "optional": {
                "audio": ("AUDIO", {"tooltip": "Audio to mux into the video."}),
                "codec": (["libx264", "libx265"], {"tooltip": "Video codec. H.264 is most compatible, H.265 is smaller."}),
                "quality": ("INT", {"default": 23, "min": 0, "max": 51, "tooltip": "CRF quality (lower = better quality, larger file). 23 is a good default."}),
            },
        }

    def save_video(self, project, frames, filename_prefix="output", fps=24.0,
                   audio=None, codec="libx264", quality=23):
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
            "optional": {
                "audio": ("AUDIO", {"tooltip": "Audio to include in the preview."}),
            },
        }

    def preview(self, frames, fps=24.0, audio=None):
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
