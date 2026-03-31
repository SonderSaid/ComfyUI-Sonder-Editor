"""LTX Editor — single node that serves as the project/scene/timeline editor.

Outputs everything the sampler needs based on the current timeline selection:
guide images, frame indices, prompt, frame count, fps, resolution, audio.
"""

import os
import logging

import cv2
import numpy as np
import torch
import folder_paths

from ..server.project_manager import load_project, list_projects, create_project, save_project
from ..server.timeline_state import TimelineProject, Scene

logger = logging.getLogger("ltx_editor")


def _make_silent_audio(duration_sec: float, sample_rate: int = 44100) -> dict:
    """Create a silent AUDIO dict compatible with ComfyUI's AUDIO format."""
    num_samples = int(duration_sec * sample_rate)
    waveform = torch.zeros(1, 2, num_samples, dtype=torch.float32)  # (batch, channels, samples)
    return {"waveform": waveform, "sample_rate": sample_rate}

CREATE_NEW = "+ Create New"


def _get_projects_base_dir():
    return os.path.join(folder_paths.get_output_directory(), "ltx_projects")


def _list_project_choices():
    """Return dropdown choices: '+ Create New' followed by existing project dir names."""
    base = _get_projects_base_dir()
    entries = [CREATE_NEW]
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if os.path.isfile(os.path.join(base, name, "project.json")):
                entries.append(name)
    return entries


def _list_scene_choices():
    """Placeholder — actual scene list is populated by the frontend."""
    return [CREATE_NEW]


class LTXEditor:
    """The LTX Editor node — project management, scene editing, and timeline
    control all in one node.

    Setup mode: create/select projects and scenes.
    Editor mode: timeline + asset gallery (handled by frontend JS).

    Outputs are computed based on the selected timeline range.
    """

    CATEGORY = "LTX-Editor"
    RETURN_TYPES = ("LTX_PROJECT", "IMAGE", "IMAGE", "STRING", "STRING", "INT", "FLOAT", "INT", "INT", "AUDIO")
    RETURN_NAMES = (
        "project", "rendered_frames", "guide_images", "guide_indices", "prompt",
        "frame_count", "fps", "width", "height", "audio",
    )
    OUTPUT_TOOLTIPS = (
        "The project object. Connect to LTX Save Video.",
        "Composited video frames from the timeline (all visible clips layered with opacity).",
        "Guide frame images as an IMAGE batch tensor. Connect to LTX guiders.",
        "Comma-separated frame indices for each guide image (e.g., '0,96').",
        "The prompt text for the selected timeline section.",
        "Number of frames in the selected section.",
        "Project FPS.",
        "Project width in pixels.",
        "Project height in pixels.",
        "Audio for the selected section (if any).",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "All-in-one editor node. Select a project and scene, set up your timeline "
        "with guide frames and prompts, then select a section to render. "
        "Outputs everything the sampler needs."
    )

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "project": (_list_project_choices(), {
                    "tooltip": "Select an existing project or '+ Create New'.",
                }),
                "project_name": ("STRING", {
                    "default": "My Project",
                    "tooltip": "Name for new project. Only used with '+ Create New'.",
                }),
                "fps": ("FLOAT", {
                    "default": 24.0, "min": 1.0, "max": 120.0, "step": 0.001,
                    "tooltip": "Frame rate. Only used when creating a new project.",
                }),
                "width": ("INT", {
                    "default": 768, "min": 64, "max": 4096, "step": 8,
                    "tooltip": "Video width. Only used when creating a new project.",
                }),
                "height": ("INT", {
                    "default": 512, "min": 64, "max": 4096, "step": 8,
                    "tooltip": "Video height. Only used when creating a new project.",
                }),
            },
            "optional": {
                # Hidden widgets — populated by the frontend JS
                "scene_id": ("STRING", {
                    "default": "",
                    "tooltip": "Active scene ID (set by editor UI).",
                }),
                "selection_start": ("INT", {
                    "default": 0, "min": 0, "max": 999999,
                    "tooltip": "Start frame of timeline selection (set by editor UI).",
                }),
                "selection_end": ("INT", {
                    "default": 0, "min": 0, "max": 999999,
                    "tooltip": "End frame of timeline selection (set by editor UI).",
                }),
            },
            "hidden": {
                "unique_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def IS_CHANGED(s, **kwargs):
        return float("nan")

    def execute(self, project, project_name, fps, width, height,
                scene_id="", selection_start=0, selection_end=0, unique_id=None):
        base_dir = _get_projects_base_dir()

        # --- Load or create project ---
        if project == CREATE_NEW:
            proj = create_project(
                name=project_name,
                fps=fps,
                width=int(width),
                height=int(height),
                base_dir=base_dir,
            )
        else:
            project_dir = os.path.join(base_dir, project)
            proj = load_project(project_dir)

        proj_fps = proj.fps
        proj_w, proj_h = proj.resolution

        # --- Find active scene ---
        scene = None
        if scene_id:
            scene = proj.get_scene(scene_id)

        # --- If no scene, return defaults ---
        if not scene:
            empty_image = torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)
            silent_audio = _make_silent_audio(1.0, 44100)
            return (proj, empty_image, empty_image, "0", "", 0, proj_fps, proj_w, proj_h, silent_audio)

        # --- Determine render range ---
        # If selection is set, use it; otherwise render the full scene
        if selection_end > selection_start:
            render_start = selection_start
            render_end = selection_end
        else:
            render_start = 0
            render_end = scene.duration_frames
            if render_end <= 0:
                empty_image = torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)
                silent_audio = _make_silent_audio(1.0, 44100)
                return (proj, empty_image, empty_image, "0", "", 0, proj_fps, proj_w, proj_h, silent_audio)

        frame_count = render_end - render_start

        # --- Render composited frames ---
        rendered_frames = self._render_scene_frames(proj, scene, render_start, render_end)

        # --- Gather guide frames within render range ---
        guide_images = []
        guide_indices = []

        for guide in scene.guide_frames:
            idx = guide.frame_index
            if idx == -1:
                idx = scene.duration_frames - 1

            if render_start <= idx < render_end:
                asset = proj.get_asset(guide.asset_id)
                if asset:
                    asset_path = os.path.join(proj.project_dir, asset.path)
                    if os.path.isfile(asset_path):
                        img = self._load_guide_image(
                            asset_path, asset.asset_type, proj_w, proj_h
                        )
                        if img is not None:
                            guide_images.append(img)
                            local_idx = idx - render_start
                            guide_indices.append(str(local_idx))

        if guide_images:
            guide_tensor = torch.stack(guide_images, dim=0)
        else:
            guide_tensor = torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)
            guide_indices = ["0"]

        indices_str = ",".join(guide_indices)

        # --- Get prompt for render range ---
        prompt_text = scene.get_prompt_for_range(render_start, render_end)

        # --- Load audio from scene's audio tracks for the render range ---
        audio = self._load_scene_audio(proj, scene, render_start, render_end)

        return (proj, rendered_frames, guide_tensor, indices_str, prompt_text, frame_count, proj_fps, proj_w, proj_h, audio)

    def _render_scene_frames(self, proj: TimelineProject, scene: Scene,
                              render_start: int, render_end: int) -> torch.Tensor:
        """Composite all visible video clips into frames for the given range.

        Returns (N, H, W, 3) float32 RGB tensor. Uses caching to skip
        re-rendering when the scene hasn't changed.
        """
        proj_w, proj_h = proj.resolution
        num_frames = render_end - render_start

        if num_frames <= 0:
            return torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)

        # --- Check cache ---
        cache_dir = os.path.join(proj.project_dir, "cache", "renders")
        content_hash = scene.content_hash(render_start, render_end, proj.resolution)
        cache_path = os.path.join(cache_dir, f"{scene.scene_id}_{content_hash}.pt")

        if os.path.isfile(cache_path):
            try:
                cached = torch.load(cache_path, weights_only=True)
                logger.info("Render cache hit for scene %s (%d frames)", scene.scene_id, num_frames)
                return cached
            except Exception as e:
                logger.warning("Failed to load render cache: %s", e)

        # --- Collect visible clips (skip hidden video lanes) ---
        hidden_lanes = set()
        for i, cfg in enumerate(scene.video_lane_configs):
            if cfg.hidden:
                hidden_lanes.add(i)

        visible_clips = [c for c in scene.clips if c.track_index not in hidden_lanes]

        if not visible_clips:
            return torch.zeros(num_frames, proj_h, proj_w, 3, dtype=torch.float32)

        # --- Open video captures (reuse across frames) ---
        captures = {}  # source_path -> cv2.VideoCapture

        def get_cap(source_path):
            abs_path = source_path
            if not os.path.isfile(abs_path):
                abs_path = os.path.join(proj.project_dir, source_path)
            if abs_path not in captures:
                cap = cv2.VideoCapture(abs_path)
                if cap.isOpened():
                    captures[abs_path] = cap
                else:
                    logger.warning("Cannot open video: %s", abs_path)
                    return None
            return captures[abs_path]

        try:
            frames = []
            for f in range(render_start, render_end):
                # Black canvas
                canvas = np.zeros((proj_h, proj_w, 3), dtype=np.uint8)

                # Find active clips at this frame, sorted by track_index (lower = bottom)
                active = [c for c in visible_clips
                          if c.timeline_start_frame <= f < c.timeline_end_frame]
                active.sort(key=lambda c: c.track_index)

                for clip in active:
                    source_frame = clip.source_in_frame + (f - clip.timeline_start_frame)
                    cap = get_cap(clip.source_path)
                    if cap is None:
                        continue

                    cap.set(cv2.CAP_PROP_POS_FRAMES, source_frame)
                    ret, frame_bgr = cap.read()
                    if not ret:
                        continue

                    # Fit frame to canvas preserving aspect ratio
                    placed = self._fit_frame_to_canvas(frame_bgr, proj_w, proj_h)

                    if clip.opacity >= 1.0:
                        # Direct composite — non-black pixels overwrite
                        mask = np.any(placed > 0, axis=2)
                        canvas[mask] = placed[mask]
                    else:
                        # Blend with opacity
                        mask = np.any(placed > 0, axis=2)
                        blended = canvas.copy()
                        blended[mask] = cv2.addWeighted(
                            canvas[mask].reshape(-1, 1, 3), 1.0 - clip.opacity,
                            placed[mask].reshape(-1, 1, 3), clip.opacity, 0
                        ).reshape(-1, 3)
                        canvas = blended

                # Convert BGR -> RGB
                frames.append(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))

            # Convert to tensor
            arr = np.stack(frames, axis=0).astype(np.float32) / 255.0
            tensor = torch.from_numpy(arr)

            # Save to cache
            os.makedirs(cache_dir, exist_ok=True)
            try:
                torch.save(tensor, cache_path)
                logger.info("Cached render for scene %s (%d frames)", scene.scene_id, num_frames)
            except Exception as e:
                logger.warning("Failed to save render cache: %s", e)

            return tensor

        finally:
            for cap in captures.values():
                cap.release()

    @staticmethod
    def _fit_frame_to_canvas(frame_bgr: np.ndarray, canvas_w: int, canvas_h: int) -> np.ndarray:
        """Resize frame to fit canvas preserving aspect ratio (letterbox/pillarbox)."""
        fh, fw = frame_bgr.shape[:2]
        scale = min(canvas_w / fw, canvas_h / fh)
        new_w = int(fw * scale)
        new_h = int(fh * scale)
        resized = cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

        canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
        x_off = (canvas_w - new_w) // 2
        y_off = (canvas_h - new_h) // 2
        canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
        return canvas

    def _load_guide_image(self, path: str, asset_type: str,
                          target_w: int, target_h: int) -> torch.Tensor | None:
        """Load an image file and return as (H, W, 3) float32 RGB tensor."""
        try:
            if asset_type == "video":
                # Extract first frame from video
                cap = cv2.VideoCapture(path)
                if not cap.isOpened():
                    return None
                ret, frame = cap.read()
                cap.release()
                if not ret:
                    return None
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            else:
                # Load image
                rgb = cv2.imread(path, cv2.IMREAD_COLOR)
                if rgb is None:
                    return None
                rgb = cv2.cvtColor(rgb, cv2.COLOR_BGR2RGB)

            # Resize to project resolution
            rgb = cv2.resize(rgb, (target_w, target_h), interpolation=cv2.INTER_LANCZOS4)

            # Convert to float32 tensor
            tensor = torch.from_numpy(rgb.astype(np.float32) / 255.0)
            return tensor

        except Exception as e:
            logger.warning("Failed to load guide image %s: %s", path, e)
            return None

    def _load_scene_audio(self, proj: TimelineProject, scene: Scene,
                          sel_start: int, sel_end: int) -> dict:
        """Load and mix audio tracks that overlap the selected frame range.

        Returns a ComfyUI AUDIO dict. Falls back to silent audio if no tracks.
        """
        duration_sec = (sel_end - sel_start) / proj.fps if proj.fps > 0 else 1.0
        sample_rate = 44100

        if not scene.audio_tracks:
            return _make_silent_audio(duration_sec, sample_rate)

        try:
            import torchaudio

            total_samples = int(duration_sec * sample_rate)
            mixed = torch.zeros(2, total_samples, dtype=torch.float32)

            # Build set of hidden audio lanes
            hidden_audio_lanes = set()
            for i, cfg in enumerate(scene.audio_lane_configs):
                if cfg.hidden:
                    hidden_audio_lanes.add(i)

            any_loaded = False
            for track in scene.audio_tracks:
                if track.muted or track.lane_index in hidden_audio_lanes:
                    continue
                # Check if track overlaps selection
                if track.timeline_end_frame <= sel_start or track.timeline_start_frame >= sel_end:
                    continue

                src_path = track.source_path
                if not os.path.isfile(src_path):
                    # Try relative to project dir
                    src_path = os.path.join(proj.project_dir, track.source_path)
                if not os.path.isfile(src_path):
                    continue

                waveform, sr = torchaudio.load(src_path)
                if sr != sample_rate:
                    waveform = torchaudio.functional.resample(waveform, sr, sample_rate)

                # Ensure stereo
                if waveform.shape[0] == 1:
                    waveform = waveform.repeat(2, 1)
                elif waveform.shape[0] > 2:
                    waveform = waveform[:2]

                # Calculate offset within the mixed buffer
                track_offset_frames = max(0, track.timeline_start_frame - sel_start)
                audio_offset_frames = max(0, sel_start - track.timeline_start_frame)

                track_offset_samples = int(track_offset_frames / proj.fps * sample_rate)
                audio_offset_samples = int(audio_offset_frames / proj.fps * sample_rate)

                # Trim source audio
                src_audio = waveform[:, audio_offset_samples:]
                available = total_samples - track_offset_samples
                if available <= 0:
                    continue
                src_audio = src_audio[:, :available]

                # Mix in with volume
                end_sample = track_offset_samples + src_audio.shape[1]
                if end_sample > total_samples:
                    src_audio = src_audio[:, :total_samples - track_offset_samples]
                    end_sample = total_samples

                mixed[:, track_offset_samples:end_sample] += src_audio * track.volume
                any_loaded = True

            if not any_loaded:
                return _make_silent_audio(duration_sec, sample_rate)

            # Clamp to prevent clipping
            mixed = mixed.clamp(-1.0, 1.0)
            return {"waveform": mixed.unsqueeze(0), "sample_rate": sample_rate}

        except ImportError:
            logger.warning("torchaudio not available — returning silent audio")
            return _make_silent_audio(duration_sec, sample_rate)
        except Exception as e:
            logger.warning("Failed to load scene audio: %s", e)
            return _make_silent_audio(duration_sec, sample_rate)
