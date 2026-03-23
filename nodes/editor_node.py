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
    RETURN_TYPES = ("LTX_PROJECT", "IMAGE", "STRING", "STRING", "INT", "FLOAT", "INT", "INT", "AUDIO")
    RETURN_NAMES = (
        "project", "guide_images", "guide_indices", "prompt",
        "frame_count", "fps", "width", "height", "audio",
    )
    OUTPUT_TOOLTIPS = (
        "The project object. Connect to LTX Save Video.",
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

        # --- If no scene or no selection, return defaults ---
        if not scene or selection_end <= selection_start:
            # Return empty/default outputs
            empty_image = torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)
            silent_audio = _make_silent_audio(1.0, 44100)
            return (proj, empty_image, "0", "", 0, proj_fps, proj_w, proj_h, silent_audio)

        # --- Compute frame count ---
        frame_count = selection_end - selection_start

        # --- Gather guide frames within selection ---
        guide_images = []
        guide_indices = []

        for guide in scene.guide_frames:
            idx = guide.frame_index
            # Resolve -1 to last frame
            if idx == -1:
                idx = scene.duration_frames - 1

            # Check if guide falls within selection
            if selection_start <= idx < selection_end:
                # Load the guide image from the asset
                asset = proj.get_asset(guide.asset_id)
                if asset:
                    asset_path = os.path.join(proj.project_dir, asset.path)
                    if os.path.isfile(asset_path):
                        img = self._load_guide_image(
                            asset_path, asset.asset_type, proj_w, proj_h
                        )
                        if img is not None:
                            guide_images.append(img)
                            # Remap to selection-local index
                            local_idx = idx - selection_start
                            guide_indices.append(str(local_idx))

        # Build guide image tensor
        if guide_images:
            guide_tensor = torch.stack(guide_images, dim=0)
        else:
            guide_tensor = torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)
            guide_indices = ["0"]

        indices_str = ",".join(guide_indices)

        # --- Get prompt for selection ---
        prompt_text = scene.get_prompt_for_range(selection_start, selection_end)

        # --- Load audio from scene's audio tracks for the selected range ---
        audio = self._load_scene_audio(proj, scene, selection_start, selection_end)

        return (proj, guide_tensor, indices_str, prompt_text, frame_count, proj_fps, proj_w, proj_h, audio)

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

            any_loaded = False
            for track in scene.audio_tracks:
                if track.muted:
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
