"""Sonder Editor — single node that serves as the project/scene/timeline editor.

Outputs everything the sampler needs based on the current timeline selection:
guide images, frame indices, prompt, frame count, fps, resolution, audio.
"""

import copy
import os
import logging
import time

import cv2
import numpy as np
import torch
import folder_paths

from ..server.project_manager import load_project, create_project, save_project
from ..server.timeline_state import GuideFrame, TimelineProject, Scene

logger = logging.getLogger("sonder_editor")


def _make_silent_audio(duration_sec: float, sample_rate: int = 44100) -> dict:
    """Create a silent AUDIO dict compatible with ComfyUI's AUDIO format."""
    num_samples = int(duration_sec * sample_rate)
    waveform = torch.zeros(1, 2, num_samples, dtype=torch.float32)  # (batch, channels, samples)
    return {"waveform": waveform, "sample_rate": sample_rate}

CREATE_NEW = "+ Create New"


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_projects_base_dir():
    return os.path.join(folder_paths.get_output_directory(), "sonder-projects")


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


class SonderEditor:
    """The Sonder Editor node — project management, scene editing, and timeline
    control all in one node.

    Setup mode: create/select projects and scenes.
    Editor mode: timeline + asset gallery (handled by frontend JS).

    Outputs are computed based on the selected timeline range.
    """

    CATEGORY = "Sonder"
    RETURN_TYPES = ("SONDER_PROJECT", "IMAGE", "IMAGE", "STRING", "STRING", "INT", "FLOAT", "INT", "INT", "AUDIO",
                     "INT", "INT", "INT", "INT", "FLOAT", "FLOAT")
    RETURN_NAMES = (
        "project", "rendered_frames", "guide_images", "guide_indices", "prompt",
        "frame_count", "fps", "width", "height", "audio",
        "context_start_frame", "context_end_frame",
        "generation_start_frame", "generation_end_frame",
        "mask_start_time", "mask_end_time",
    )
    OUTPUT_TOOLTIPS = (
        "The project object. Connect to Sonder Save Video.",
        "Composited video frames from the timeline (all visible clips layered with opacity).",
        "Guide frame images as an IMAGE batch tensor. Connect to LTX guiders.",
        "Comma-separated frame indices for each guide image (e.g., '0,96').",
        "The prompt text for the selected timeline section.",
        "Number of frames in the selected section.",
        "Project FPS.",
        "Project width in pixels.",
        "Project height in pixels.",
        "Audio for the selected section (if any).",
        "Absolute frame where context begins (includes context padding).",
        "Absolute frame where context ends (includes context padding).",
        "Absolute frame where new generation starts (original selection start).",
        "Absolute frame where new generation ends (original selection end).",
        "Seconds offset within output where generation starts. Wire to temporal mask start_time.",
        "Seconds offset within output where generation ends. Wire to temporal mask end_time.",
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
                "pre_context_frames": ("INT", {
                    "default": 0, "min": 0, "max": 256, "step": 1,
                    "tooltip": "Context frames BEFORE selection. Included in render but not denoised (use mask outputs). Clamped to available frames.",
                }),
                "post_context_frames": ("INT", {
                    "default": 0, "min": 0, "max": 256, "step": 1,
                    "tooltip": "Context frames AFTER selection. Included in render but not denoised (use mask outputs). Clamped to available frames.",
                }),
            },
            "hidden": {
                "prompt": "PROMPT",
                "unique_id": "UNIQUE_ID",
            },
        }

    @classmethod
    def IS_CHANGED(s, **kwargs):
        return float("nan")

    @staticmethod
    def _empty_execute_result(proj: TimelineProject, proj_fps: float, proj_w: int, proj_h: int):
        empty_image = torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)
        silent_audio = _make_silent_audio(1.0, 44100)
        return (
            proj,
            empty_image,
            empty_image,
            "0",
            "",
            0,
            proj_fps,
            proj_w,
            proj_h,
            silent_audio,
            0,
            0,
            0,
            0,
            0.0,
            0.0,
        )

    @staticmethod
    def _queue_snapshot_version(queue_job) -> int:
        if not queue_job:
            return 0
        params = getattr(queue_job, "params", {}) or {}
        if not isinstance(params, dict):
            return 0
        return max(0, _coerce_int(params.get("snapshot_version", 0), 0))

    @staticmethod
    def _prompt_linked_node_id(value):
        if not isinstance(value, (list, tuple)) or not value:
            return None
        node_id = value[0]
        if isinstance(node_id, (str, int)):
            return str(node_id)
        return None

    @staticmethod
    def _prompt_bool(value) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return False

    @classmethod
    def _prompt_node_inputs(cls, prompt, node_id):
        if not isinstance(prompt, dict):
            return {}
        node = prompt.get(str(node_id))
        if not isinstance(node, dict):
            node = prompt.get(node_id)
        if not isinstance(node, dict):
            return {}
        inputs = node.get("inputs", {})
        return inputs if isinstance(inputs, dict) else {}

    @classmethod
    def _execution_reaches_terminal_save(cls, prompt, unique_id) -> bool:
        if not isinstance(prompt, dict) or unique_id in {None, ""}:
            return False
        target_id = str(unique_id)
        for node in prompt.values():
            if not isinstance(node, dict):
                continue
            if node.get("class_type") not in {"SonderSaveVideo", "SonderSaveBridge"}:
                continue
            inputs = node.get("inputs", {})
            if not isinstance(inputs, dict):
                continue
            if not cls._prompt_bool(inputs.get("mark_queue_complete")):
                continue
            project_source = cls._prompt_linked_node_id(inputs.get("project"))
            if project_source is None:
                continue
            stack = [project_source]
            seen = set()
            while stack:
                current_id = stack.pop()
                if current_id == target_id:
                    return True
                if current_id in seen:
                    continue
                seen.add(current_id)
                for upstream in cls._prompt_node_inputs(prompt, current_id).values():
                    upstream_id = cls._prompt_linked_node_id(upstream)
                    if upstream_id and upstream_id not in seen:
                        stack.append(upstream_id)
        return False

    def _consume_queue_job(self, proj: TimelineProject):
        queue = getattr(proj, "generation_queue", []) or []
        # A new consume pass means a fresh ComfyUI prompt execution. Any prior
        # running job therefore never completed and must be retried.
        for job in queue:
            if (job.status or "pending").lower() == "running":
                job.status = "pending"
                job.error = ""
                job.progress = 0.0
        for job in queue:
            if (job.status or "pending").lower() != "pending":
                continue
            job.status = "running"
            job.error = ""
            job.progress = 0.0
            save_project(proj)
            return job
        return None

    def _mark_later_batch_jobs_failed(self, proj: TimelineProject, queue_job):
        batch_id = str(getattr(queue_job, "batch_id", "") or "")
        if not proj or not batch_id:
            return

        queue = getattr(proj, "generation_queue", []) or []
        failed_queue_index = None
        failed_job_id = str(getattr(queue_job, "job_id", "") or "")
        failed_batch_index = _coerce_int(getattr(queue_job, "batch_index", 0), 0)

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

            job_batch_index = _coerce_int(getattr(job, "batch_index", 0), 0)
            is_later_chunk = job_batch_index > failed_batch_index
            if failed_queue_index is not None and idx > failed_queue_index:
                is_later_chunk = True
            if not is_later_chunk:
                continue

            job.status = "failed"
            job.error = skip_error
            job.progress = 0.0

    def _mark_queue_job_failed(self, proj: TimelineProject, queue_job, error_message: str):
        if not proj or not queue_job:
            return
        queue_job.status = "failed"
        queue_job.error = str(error_message)
        queue_job.progress = 0.0
        self._mark_later_batch_jobs_failed(proj, queue_job)
        save_project(proj)

    def execute(self, project, project_name, fps, width, height,
                scene_id="", selection_start=0, selection_end=0,
                pre_context_frames=0, post_context_frames=0,
                prompt=None, unique_id=None):
        base_dir = _get_projects_base_dir()
        execute_started_at = time.perf_counter()
        selection_start = max(0, _coerce_int(selection_start, 0))
        selection_end = max(0, _coerce_int(selection_end, 0))
        pre_context_frames = max(0, _coerce_int(pre_context_frames, 0))
        post_context_frames = max(0, _coerce_int(post_context_frames, 0))
        proj = None
        queue_job = None

        try:
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

            if self._execution_reaches_terminal_save(prompt, unique_id):
                queue_job = self._consume_queue_job(proj)
            snapshot_version = self._queue_snapshot_version(queue_job)
            if queue_job:
                scene_id = queue_job.scene_id or scene_id
                selection_start = max(0, _coerce_int(getattr(queue_job, "selection_start", 0), 0))
                selection_end = max(0, _coerce_int(getattr(queue_job, "selection_end", 0), 0))
                pre_context_frames = max(0, _coerce_int(getattr(queue_job, "pre_context_frames", 0), 0))
                post_context_frames = max(0, _coerce_int(getattr(queue_job, "post_context_frames", 0), 0))
            logger.info(
                "execute begin: scene_id=%s selection=%d-%d",
                scene_id or "",
                selection_start,
                selection_end,
            )

            # --- Find active scene ---
            scene = None
            if scene_id:
                scene = proj.get_scene(scene_id)

            if queue_job and not scene:
                raise RuntimeError(f"Queued scene not found: {scene_id}")

            # New queue jobs freeze scene-level overrides even when the live scene changes.
            if scene and queue_job and snapshot_version > 0:
                scene = copy.deepcopy(scene)
                scene.width = max(0, _coerce_int(getattr(queue_job, "scene_width", 0), 0))
                scene.height = max(0, _coerce_int(getattr(queue_job, "scene_height", 0), 0))
                try:
                    scene.fps = max(0.0, float(getattr(queue_job, "scene_fps", 0.0) or 0.0))
                except (TypeError, ValueError):
                    scene.fps = 0.0

            # --- Scene-level overrides (resolution + fps) ---
            if scene and scene.width > 0:
                proj_w = scene.width
            if scene and scene.height > 0:
                proj_h = scene.height
            if scene and hasattr(scene, "fps") and scene.fps > 0:
                proj_fps = scene.fps

            # --- If no scene, return defaults ---
            if not scene:
                logger.info(
                    "execute end: scene_id=%s frames=%d duration=%.2fs",
                    scene_id or "",
                    0,
                    time.perf_counter() - execute_started_at,
                )
                return self._empty_execute_result(proj, proj_fps, proj_w, proj_h)

            # --- Determine render range ---
            # If selection is set, use it; otherwise render the full scene
            if selection_end > selection_start:
                render_start = selection_start
                render_end = selection_end
                if queue_job:
                    render_end = min(render_end, scene.duration_frames)
                    if render_end <= render_start:
                        raise RuntimeError(
                            f"Queued selection is outside scene bounds: {selection_start}-{selection_end}"
                        )
            else:
                render_start = 0
                render_end = scene.duration_frames
                if render_end <= 0:
                    if queue_job:
                        raise RuntimeError(f"Queued scene has no renderable duration: {scene.name}")
                    return self._empty_execute_result(proj, proj_fps, proj_w, proj_h)

            # --- Context frame expansion (asymmetric pre/post) ---
            # generation_start/end = the actual new frames to generate (original selection)
            # context extends the render range to include surrounding frames for temporal consistency
            generation_start = render_start
            generation_end = render_end
            actual_pre = min(pre_context_frames, generation_start)
            actual_post = min(post_context_frames, scene.duration_frames - generation_end)
            if actual_pre > 0 or actual_post > 0:
                render_start = generation_start - actual_pre
                render_end = generation_end + actual_post

            context_start = render_start
            context_end = render_end
            frame_count = render_end - render_start

            # Mask times - seconds offset within the output tensor for downstream temporal masks
            mask_start_time = (generation_start - context_start) / proj_fps if proj_fps > 0 else 0.0
            mask_end_time = (generation_end - context_start) / proj_fps if proj_fps > 0 else 0.0

            # --- Render composited frames ---
            render_started_at = time.perf_counter()
            logger.info("render start: scene_id=%s range=%d-%d", scene.scene_id, render_start, render_end)
            rendered_frames = self._render_scene_frames(proj, scene, render_start, render_end)
            logger.info(
                "render end: scene_id=%s frames=%d duration=%.2fs",
                scene.scene_id,
                frame_count,
                time.perf_counter() - render_started_at,
            )

            # --- Gather guide frames within render range ---
            guide_images = []
            guide_indices = []
            guide_frames = scene.guide_frames
            if queue_job and snapshot_version > 0:
                guide_frames = [
                    GuideFrame.from_dict(guide)
                    for guide in getattr(queue_job, "guide_frame_snapshots", [])
                    if isinstance(guide, dict)
                ]

            for guide in guide_frames:
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
            if queue_job:
                prompt_text = getattr(queue_job, "prompt", "")
                if snapshot_version <= 0 and not prompt_text:
                    prompt_text = scene.get_prompt_for_range(render_start, render_end)
            else:
                prompt_text = scene.get_prompt_for_range(render_start, render_end)

            # --- Load audio from scene's audio tracks for the render range ---
            audio = self._load_scene_audio(proj, scene, render_start, render_end)

            # --- Attach execution context for downstream nodes (e.g., SonderSaveVideo Take mode) ---
            proj._execution_context = {
                "scene_id": scene.scene_id,
                "scene_name": scene.name,
                "selection_start": generation_start,
                "selection_end": generation_end,
                "context_start": context_start,
                "context_end": context_end,
                "pre_context_frames": actual_pre,
                "post_context_frames": actual_post,
                "prompt": prompt_text,
                "queue_job_id": queue_job.job_id if queue_job else "",
            }
            logger.info(
                "execute end: scene_id=%s frames=%d duration=%.2fs",
                scene.scene_id,
                frame_count,
                time.perf_counter() - execute_started_at,
            )

            return (
                proj,
                rendered_frames,
                guide_tensor,
                indices_str,
                prompt_text,
                frame_count,
                proj_fps,
                proj_w,
                proj_h,
                audio,
                context_start,
                context_end,
                generation_start,
                generation_end,
                mask_start_time,
                mask_end_time,
            )
        except Exception as e:
            logger.warning(
                "execute failed: scene_id=%s duration=%.2fs error=%s",
                scene_id or "",
                time.perf_counter() - execute_started_at,
                e,
            )
            if proj is not None and queue_job is not None:
                self._mark_queue_job_failed(proj, queue_job, str(e))
            raise

    def _render_scene_frames(self, proj: TimelineProject, scene: Scene,
                              render_start: int, render_end: int) -> torch.Tensor:
        """Composite all visible video clips into frames for the given range.

        Returns (N, H, W, 3) float32 RGB tensor. Uses caching to skip
        re-rendering when the scene hasn't changed.
        """
        proj_w, proj_h = proj.resolution
        # Scene-level resolution override
        if scene.width > 0:
            proj_w = scene.width
        if scene.height > 0:
            proj_h = scene.height
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
                try:
                    os.remove(cache_path)
                    logger.warning("Deleted corrupt render cache: %s", cache_path)
                except OSError as remove_error:
                    logger.warning("Failed to delete corrupt render cache %s: %s", cache_path, remove_error)

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
                    # BUG-2 fix: use placement bounds, not pixel values, for compositing
                    placed, (dx, dy, dw, dh) = self._fit_frame_to_canvas(frame_bgr, proj_w, proj_h)

                    if clip.opacity >= 1.0:
                        # Direct composite — copy content region (black pixels preserved)
                        canvas[dy:dy + dh, dx:dx + dw] = placed[dy:dy + dh, dx:dx + dw]
                    else:
                        # Blend with opacity within content region only
                        roi_canvas = canvas[dy:dy + dh, dx:dx + dw]
                        roi_placed = placed[dy:dy + dh, dx:dx + dw]
                        canvas[dy:dy + dh, dx:dx + dw] = cv2.addWeighted(
                            roi_canvas, 1.0 - clip.opacity,
                            roi_placed, clip.opacity, 0
                        )

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
    def _fit_frame_to_canvas(frame_bgr: np.ndarray, canvas_w: int, canvas_h: int):
        """Resize frame to fit canvas preserving aspect ratio (letterbox/pillarbox).

        Returns:
            tuple: (canvas, (x_off, y_off, new_w, new_h)) — placed frame and content bounds.
        """
        fh, fw = frame_bgr.shape[:2]
        scale = min(canvas_w / fw, canvas_h / fh)
        new_w = int(fw * scale)
        new_h = int(fh * scale)
        resized = cv2.resize(frame_bgr, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)

        canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
        x_off = (canvas_w - new_w) // 2
        y_off = (canvas_h - new_h) // 2
        canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
        return canvas, (x_off, y_off, new_w, new_h)

    def _load_guide_image(self, path: str, asset_type: str,
                          target_w: int, target_h: int) -> torch.Tensor | None:
        """Load an image file and return as (H, W, 3) float32 RGB tensor."""
        cap = None
        try:
            if asset_type == "video":
                # Extract first frame from video
                cap = cv2.VideoCapture(path)
                if not cap.isOpened():
                    return None
                ret, frame = cap.read()
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
        finally:
            if cap is not None:
                cap.release()

    def _load_scene_audio(self, proj: TimelineProject, scene: Scene,
                          sel_start: int, sel_end: int) -> dict:
        """Load and mix audio tracks that overlap the selected frame range.

        Returns a ComfyUI AUDIO dict. Falls back to silent audio if no tracks.
        """
        # Scene-level fps override
        effective_fps = scene.fps if hasattr(scene, 'fps') and scene.fps > 0 else proj.fps
        duration_sec = (sel_end - sel_start) / effective_fps if effective_fps > 0 else 1.0
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
            considered_tracks = len(scene.audio_tracks)
            loaded_tracks = 0
            for track in scene.audio_tracks:
                if track.muted:
                    logger.debug("Skipping scene audio track %s: muted", track.source_path)
                    continue
                if track.lane_index in hidden_audio_lanes:
                    logger.debug(
                        "Skipping scene audio track %s: hidden lane %s",
                        track.source_path,
                        track.lane_index,
                    )
                    continue
                # Check if track overlaps selection
                if track.timeline_end_frame <= sel_start or track.timeline_start_frame >= sel_end:
                    logger.debug(
                        "Skipping scene audio track %s: no overlap with range %d-%d",
                        track.source_path,
                        sel_start,
                        sel_end,
                    )
                    continue

                raw_path = track.source_path
                fallback_path = os.path.join(proj.project_dir, track.source_path)
                src_path = raw_path
                if not os.path.isfile(src_path):
                    # Try relative to project dir
                    src_path = fallback_path
                if not os.path.isfile(src_path):
                    logger.info(
                        "Skipping scene audio track %s: file not found (attempted: %s, %s)",
                        track.source_path,
                        raw_path,
                        fallback_path,
                    )
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

                track_offset_samples = int(track_offset_frames / effective_fps * sample_rate)
                # BUG-3 fix: include source_in_frame for trimmed/split audio tracks
                source_offset_frames = track.source_in_frame + audio_offset_frames
                audio_offset_samples = int(source_offset_frames / effective_fps * sample_rate)

                # Trim source audio
                src_audio = waveform[:, audio_offset_samples:]
                available = total_samples - track_offset_samples
                if available <= 0:
                    logger.debug(
                        "Skipping scene audio track %s: no buffer space after offsets",
                        track.source_path,
                    )
                    continue
                src_audio = src_audio[:, :available]

                # Mix in with volume
                end_sample = track_offset_samples + src_audio.shape[1]
                if end_sample > total_samples:
                    src_audio = src_audio[:, :total_samples - track_offset_samples]
                    end_sample = total_samples

                mixed[:, track_offset_samples:end_sample] += src_audio * track.volume
                any_loaded = True
                loaded_tracks += 1

            if not any_loaded:
                logger.info(
                    "Scene audio fell back to silence: considered=%d loaded=%d range=%d-%d",
                    considered_tracks,
                    loaded_tracks,
                    sel_start,
                    sel_end,
                )
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
