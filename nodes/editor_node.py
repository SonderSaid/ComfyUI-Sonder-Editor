"""Sonder Editor — single node that serves as the project/scene/timeline editor.

Outputs everything the sampler needs based on the current timeline selection:
guide images, frame indices, prompt, frame count, fps, resolution, audio.
"""

import copy
import math
import os
import logging
import time

import cv2
import numpy as np
import torch
import folder_paths

from ..server.project_manager import ProjectVersionConflict, load_project, create_project, save_project
from ..server.timeline_state import GuideFrame, TimelineProject, Scene
from ..server.media_helpers import decode_audio_samples, decode_video_frame, fit_frame_to_canvas
from ..server.path_security import (
    PathSecurityError,
    path_within,
    resolve_existing_project_path,
    safe_route_token,
)
from ..server.timeline_renderer import render_scene_frames

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


def _resolve_project_choice_dir(base_dir: str, project: str) -> str:
    try:
        project_id = safe_route_token(project, "project")
    except PathSecurityError as exc:
        raise ValueError(str(exc)) from exc

    base_real = os.path.realpath(base_dir)
    project_dir = os.path.realpath(os.path.join(base_real, project_id))
    if not path_within(base_real, project_dir):
        raise ValueError("Project path escapes configured base directory")
    return project_dir


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
    RETURN_TYPES = ("SONDER_PROJECT", "IMAGE", "IMAGE", "STRING", "STRING",
                    "STRING", "INT", "FLOAT", "INT", "INT", "AUDIO", "FLOAT", "FLOAT")
    RETURN_NAMES = (
        "project", "rendered_frames", "guide_images", "guide_idx", "guide_strengths",
        "prompt", "frame_count", "fps", "width", "height", "audio",
        "mask_start_time", "mask_end_time",
    )
    OUTPUT_TOOLTIPS = (
        "The project object. Connect to Sonder Save Video.",
        "Composited video frames from the timeline (all visible clips layered with opacity).",
        "Guide frame images as an IMAGE batch tensor. Connect to LTX guiders.",
        "Comma-separated frame indices for each guide image (e.g., '0,96').",
        "Comma-separated guide strengths aligned with guide_idx.",
        "The prompt text for the selected timeline section.",
        "Number of frames in the selected section.",
        "Project FPS.",
        "Project width in pixels.",
        "Project height in pixels.",
        "Audio for the selected section (if any).",
        "Seconds offset within output where generation starts. Wire to temporal mask start_time.",
        "Seconds offset within output where generation ends. Wire to temporal mask end_time.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Build and render video scenes with timeline clips, guides, prompts, takes, and project assets."
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
                "mask_pre_offset": ("INT", {
                    "default": 0, "min": 0, "max": 256, "step": 1,
                    "tooltip": "Extra pre-context frames to exclude from denoise mask start. Clamped to actual pre-context.",
                }),
                "mask_post_offset": ("INT", {
                    "default": 0, "min": 0, "max": 256, "step": 1,
                    "tooltip": "Extra post-context frames to include in denoise mask end. Clamped to actual post-context.",
                }),
                "take_placement_mode": ("STRING", {
                    "default": "trimmed",
                    "tooltip": "Take placement mode for non-queued renders. Set by editor settings.",
                }),
                "take_placement_linked": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "Whether take video/audio siblings are linked when placed on the timeline.",
                }),
                "take_placement_muted": ("BOOLEAN", {
                    "default": False,
                    "tooltip": "Whether newly placed takes enter the timeline muted.",
                }),
                "render_queue_active": ("BOOLEAN", {
                    "default": True,
                    "tooltip": "When enabled, queue jobs drive editor execution. Disable to render the live selection without consuming queue jobs.",
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
            "",
            "",
            "",
            0,
            proj_fps,
            proj_w,
            proj_h,
            silent_audio,
            0.0,
            0.0,
        )

    @staticmethod
    def _execution_template_id(proj: TimelineProject, queue_job=None) -> str:
        if queue_job:
            template_id = str(getattr(queue_job, "template_id", "") or "")
            if template_id:
                return template_id
        return str(getattr(proj, "template_id", "") or "free")

    @staticmethod
    def _execution_frame_constraint(proj: TimelineProject, queue_job=None) -> dict | None:
        if queue_job:
            job_constraint = getattr(queue_job, "frame_constraint", None)
            if isinstance(job_constraint, dict) and job_constraint:
                return job_constraint
        proj_constraint = getattr(proj, "frame_constraint", None)
        if isinstance(proj_constraint, dict) and proj_constraint:
            return proj_constraint
        return None

    @staticmethod
    def _round_up_frame_count(frame_count: int, frame_constraint: dict | None) -> int:
        count = max(0, _coerce_int(frame_count, 0))
        if not frame_constraint or "step" not in frame_constraint or count <= 0:
            return count
        step = max(1, _coerce_int(frame_constraint.get("step"), 1))
        offset = _coerce_int(frame_constraint.get("offset"), 0)
        minimum = max(1, _coerce_int(frame_constraint.get("min"), 1))
        count = max(count, minimum)
        if (count - offset) % step == 0:
            return count
        return offset + math.ceil((count - offset) / step) * step

    @staticmethod
    def _snap_pixel_to_constraint(pixel: int, frame_constraint: dict | None, side: str) -> int:
        # Boundary set in pixel-frame space: {0} ∪ {step*k + offset : k ≥ 0}.
        # side="start" floors to boundary ≤ pixel; side="end" ceils to boundary ≥ pixel.
        # No-op when constraint is missing or step <= 1, so "free" template scenes are unchanged.
        # Used to align mask_start_time / mask_end_time so downstream latent maskers
        # (e.g. kjnodes LTXVAudioVideoMask) include the full requested generation region.
        if not frame_constraint or "step" not in frame_constraint:
            return pixel
        step = max(1, _coerce_int(frame_constraint.get("step"), 1))
        offset = _coerce_int(frame_constraint.get("offset"), 0)
        if step <= 1:
            return pixel
        if pixel <= 0:
            return 0
        if pixel < offset:
            return 0 if side == "start" else offset
        k = (pixel - offset) / step
        if side == "start":
            return offset + math.floor(k) * step
        return offset + math.ceil(k) * step

    @staticmethod
    def _snap_mask_pre_offset_up(value: int, actual_pre: int, step: int) -> int:
        # Valid set: {0, step, 2*step, ..., k*step where k*step <= actual_pre} ∪ {actual_pre}.
        # The "∪ {actual_pre}" full-mask option is needed because actual_pre on the LTX
        # grid (e.g. 25 = step*3 + offset) is NOT itself a multiple of step.
        # Returns 0 when actual_pre <= 0 or value <= 0. Clamps down to actual_pre when value > actual_pre.
        value = _coerce_int(value, 0)
        actual_pre = _coerce_int(actual_pre, 0)
        step = max(1, _coerce_int(step, 1))
        if actual_pre <= 0 or value <= 0:
            return 0
        if value >= actual_pre:
            return actual_pre
        snapped = math.ceil(value / step) * step
        if snapped <= actual_pre:
            return snapped
        return actual_pre

    @staticmethod
    def _snap_mask_post_offset_up(value: int, actual_post: int, step: int) -> int:
        # Valid set: {0, step, 2*step, ..., k*step where k*step <= actual_post}.
        # Post side does not need the full-context include (actual_post is itself a
        # multiple of step by construction since it doesn't carry the +1 offset).
        # Returns 0 when actual_post <= 0 or value <= 0. Clamps down to actual_post when value > actual_post.
        value = _coerce_int(value, 0)
        actual_post = _coerce_int(actual_post, 0)
        step = max(1, _coerce_int(step, 1))
        if actual_post <= 0 or value <= 0:
            return 0
        if value >= actual_post:
            return actual_post
        snapped = math.ceil(value / step) * step
        if snapped <= actual_post:
            return snapped
        return actual_post

    @staticmethod
    def _pad_image_batch_to_frame_count(frames: torch.Tensor, target_count: int) -> torch.Tensor:
        if not torch.is_tensor(frames) or frames.ndim < 1:
            return frames
        current_count = int(frames.shape[0])
        pad_count = max(0, _coerce_int(target_count, current_count) - current_count)
        if pad_count <= 0 or current_count <= 0:
            return frames
        repeat_shape = [pad_count] + [1] * (frames.ndim - 1)
        return torch.cat([frames, frames[-1:].repeat(*repeat_shape)], dim=0)

    @staticmethod
    def _pad_audio_to_frame_count(audio: dict, target_frame_count: int, fps: float) -> dict:
        if not isinstance(audio, dict) or fps <= 0:
            return audio
        waveform = audio.get("waveform")
        sample_rate = _coerce_int(audio.get("sample_rate"), 0)
        if not torch.is_tensor(waveform) or sample_rate <= 0:
            return audio
        target_samples = math.ceil((max(0, target_frame_count) / fps) * sample_rate)
        current_samples = int(waveform.shape[-1])
        if current_samples >= target_samples:
            return audio
        pad_shape = list(waveform.shape)
        pad_shape[-1] = target_samples - current_samples
        padded = torch.cat([waveform, waveform.new_zeros(pad_shape)], dim=-1)
        next_audio = dict(audio)
        next_audio["waveform"] = padded
        return next_audio

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

    @staticmethod
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
    def _execution_reaches_save_with_mark(cls, prompt, unique_id, expected_mark_queue_complete: bool) -> bool:
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
            if cls._prompt_bool(inputs.get("mark_queue_complete")) is not expected_mark_queue_complete:
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

    @classmethod
    def _execution_reaches_terminal_save(cls, prompt, unique_id) -> bool:
        return cls._execution_reaches_save_with_mark(prompt, unique_id, True)

    @classmethod
    def _execution_targets_unmarked_save(cls, prompt, unique_id) -> bool:
        return cls._execution_reaches_save_with_mark(prompt, unique_id, False)

    @staticmethod
    def _peek_queue_job(proj: TimelineProject):
        queue = getattr(proj, "generation_queue", []) or []
        for desired_status in ("running", "pending"):
            for job in queue:
                if (getattr(job, "status", "pending") or "pending").lower() == desired_status:
                    return job
        return None

    def _consume_queue_job(self, proj: TimelineProject):
        last_conflict = None
        for _attempt in range(3):
            queue = getattr(proj, "generation_queue", []) or []
            base_modified_at = getattr(proj, "modified_at", "")
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
                job.base_modified_at = base_modified_at
                try:
                    if base_modified_at:
                        save_project(proj, expected_modified_at=base_modified_at)
                    else:
                        save_project(proj)
                except ProjectVersionConflict as exc:
                    last_conflict = exc
                    proj = load_project(proj.project_dir)
                    break
                return proj, job
            else:
                return proj, None
        raise RuntimeError("Queue job claim conflicted with an editor save; retry the prompt.") from last_conflict

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
        job_id = str(getattr(queue_job, "job_id", "") or "")
        last_conflict = None
        for _attempt in range(3):
            base_modified_at = getattr(proj, "modified_at", "")
            target = queue_job
            if job_id:
                target = next(
                    (job for job in (getattr(proj, "generation_queue", []) or []) if getattr(job, "job_id", "") == job_id),
                    queue_job,
                )
            target.status = "failed"
            target.error = str(error_message)
            target.progress = 0.0
            self._mark_later_batch_jobs_failed(proj, target)
            try:
                if base_modified_at:
                    save_project(proj, expected_modified_at=base_modified_at)
                else:
                    save_project(proj)
                return
            except ProjectVersionConflict as exc:
                last_conflict = exc
                proj = load_project(proj.project_dir)
        raise RuntimeError("Failed to mark queue job failed after concurrent editor saves.") from last_conflict

    def execute(self, project, project_name, fps, width, height,
                scene_id="", selection_start=0, selection_end=0,
                pre_context_frames=0, post_context_frames=0,
                mask_pre_offset=0, mask_post_offset=0,
                prompt=None, unique_id=None, take_placement_mode="trimmed",
                take_placement_linked=True, take_placement_muted=False,
                render_queue_active=True):
        base_dir = _get_projects_base_dir()
        execute_started_at = time.perf_counter()
        selection_start = max(0, _coerce_int(selection_start, 0))
        selection_end = max(0, _coerce_int(selection_end, 0))
        pre_context_frames = max(0, _coerce_int(pre_context_frames, 0))
        post_context_frames = max(0, _coerce_int(post_context_frames, 0))
        mask_pre_offset = max(0, _coerce_int(mask_pre_offset, 0))
        mask_post_offset = max(0, _coerce_int(mask_post_offset, 0))
        take_placement_mode = take_placement_mode if take_placement_mode in ("trimmed", "untrimmed") else "trimmed"
        take_placement_linked = self._coerce_bool(take_placement_linked, True)
        take_placement_muted = self._coerce_bool(take_placement_muted, False)
        render_queue_active = self._coerce_bool(render_queue_active, True)
        proj = None
        queue_job = None
        queue_job_consumed = False

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
                project_dir = _resolve_project_choice_dir(base_dir, project)
                proj = load_project(project_dir)

            proj_fps = proj.fps
            proj_w, proj_h = proj.resolution
            execution_base_modified_at = getattr(proj, "modified_at", "")

            terminal_save_reached = self._execution_reaches_terminal_save(prompt, unique_id)
            unmarked_save_reached = self._execution_targets_unmarked_save(prompt, unique_id)
            queue_length = len(getattr(proj, "generation_queue", []) or [])
            queue_job_mode = ""
            if render_queue_active and terminal_save_reached:
                proj, queue_job = self._consume_queue_job(proj)
                queue_job_consumed = queue_job is not None
                queue_job_mode = "consume" if queue_job else ""
            elif render_queue_active:
                # Peek the queued snapshot for any non-consume queue-active case,
                # including when no save node is wired (e.g. preview/show-any-only
                # quick tests). Peek renders the queued range without claiming,
                # completing, or otherwise mutating the job or the project.
                queue_job = self._peek_queue_job(proj)
                queue_job_mode = "peek" if queue_job else ""
            snapshot_version = self._queue_snapshot_version(queue_job)
            if queue_job:
                execution_base_modified_at = getattr(queue_job, "base_modified_at", "") or getattr(proj, "modified_at", "")
            if queue_job:
                scene_id = queue_job.scene_id or scene_id
                selection_start = max(0, _coerce_int(getattr(queue_job, "selection_start", 0), 0))
                selection_end = max(0, _coerce_int(getattr(queue_job, "selection_end", 0), 0))
                pre_context_frames = max(0, _coerce_int(getattr(queue_job, "pre_context_frames", 0), 0))
                post_context_frames = max(0, _coerce_int(getattr(queue_job, "post_context_frames", 0), 0))
                mask_pre_offset = max(0, _coerce_int(getattr(queue_job, "mask_pre_offset", 0), 0))
                mask_post_offset = max(0, _coerce_int(getattr(queue_job, "mask_post_offset", 0), 0))
            logger.info(
                "execute begin: scene_id=%s selection=%d-%d terminal_save=%s unmarked_save=%s render_queue_active=%s queue_length=%d queue_job_mode=%s queue_job_id=%s snapshot_range=%s-%s",
                scene_id or "",
                selection_start,
                selection_end,
                terminal_save_reached,
                unmarked_save_reached,
                render_queue_active,
                queue_length,
                queue_job_mode,
                getattr(queue_job, "job_id", "") if queue_job else "",
                getattr(queue_job, "selection_start", "") if queue_job else "",
                getattr(queue_job, "selection_end", "") if queue_job else "",
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
                if queue_job:
                    raise RuntimeError(
                        f"Queued selection has zero or invalid range: {selection_start}-{selection_end}"
                    )
                render_start = 0
                render_end = scene.duration_frames
                if render_end <= 0:
                    return self._empty_execute_result(proj, proj_fps, proj_w, proj_h)

            # --- Context frame expansion (asymmetric pre/post) ---
            # generation_start/end = the actual new frames to generate (original selection)
            # context extends the render range to include surrounding frames for temporal consistency
            generation_start = render_start
            generation_end = render_end
            actual_pre = min(pre_context_frames, generation_start)
            actual_post = min(post_context_frames, scene.duration_frames - generation_end)

            template_id = self._execution_template_id(proj, queue_job)
            frame_constraint = self._execution_frame_constraint(proj, queue_job)

            # Independent snaps for the four sections — mask offsets do NOT inflate the
            # rendered tensor (the bug fixed here). The LTX +1 (offset=1) lives once at
            # the start of the total tensor and is carried by `actual_pre` when pre > 0,
            # or by `gen_len` via the existing selection-endpoint snap when pre == 0.
            # post never carries the +1. Mask offsets snap to the valid grid-difference
            # set within their context cap — they only choose which already-rendered
            # frames are masked, not how many frames get rendered.
            #
            # Scene-edge fallback: when expansion can't reach the next grid value
            # because of scene bounds, expansion is skipped (all-or-nothing) and the
            # `_snap_pixel_to_constraint` helper below fires on the mask boundary as a
            # last resort. In the common case the helper is a no-op since boundaries
            # are already on grid by construction.
            if frame_constraint and _coerce_int(frame_constraint.get("step"), 1) > 1:
                step = max(1, _coerce_int(frame_constraint.get("step"), 1))
                # 1. actual_pre -> next G value (when pre > 0).
                if actual_pre > 0:
                    grid_aligned_pre = self._snap_pixel_to_constraint(
                        actual_pre, frame_constraint, "end"
                    )
                    pre_extension = max(0, grid_aligned_pre - actual_pre)
                    if pre_extension <= (generation_start - actual_pre):
                        actual_pre += pre_extension
                # 2. actual_post -> next multiple of step.
                post_remainder = actual_post % step
                post_extension = (step - post_remainder) % step
                if post_extension <= (scene.duration_frames - generation_end - actual_post):
                    actual_post += post_extension
                # 3 & 4. Mask offsets snap to their valid sets within the (now snapped)
                # context caps. These calls absorb the previous pre-snap clamp.
                mask_pre_offset = self._snap_mask_pre_offset_up(
                    mask_pre_offset, actual_pre, step
                )
                mask_post_offset = self._snap_mask_post_offset_up(
                    mask_post_offset, actual_post, step
                )
            else:
                # No template constraint: just clamp offsets to context bounds (old behavior).
                mask_pre_offset = max(0, min(mask_pre_offset, actual_pre))
                mask_post_offset = max(0, min(mask_post_offset, actual_post))

            if actual_pre > 0 or actual_post > 0:
                render_start = generation_start - actual_pre
                render_end = generation_end + actual_post

            context_start = render_start
            context_end = render_end
            source_frame_count = render_end - render_start
            frame_count = source_frame_count
            target_frame_count = self._round_up_frame_count(frame_count, frame_constraint)
            frame_count_padding = max(0, target_frame_count - frame_count)
            logger.info(
                "render frame plan: scene_id=%s template=%s constraint=%s source_frames=%d padded_frames=%d padding=%d",
                scene.scene_id,
                template_id,
                frame_constraint or "none",
                source_frame_count,
                target_frame_count,
                frame_count_padding,
            )

            # Mask times - seconds offset within the output tensor for downstream temporal masks.
            # Boundaries are snapped outward to the active template's frame constraint grid
            # (start floors, end ceils) so latent maskers like LTXVAudioVideoMask include the
            # full requested generation region without dropping the straddling boundary latent.
            mask_start_pixel = generation_start - context_start - mask_pre_offset
            mask_end_pixel = generation_end - context_start + mask_post_offset + frame_count_padding
            mask_start_pixel = max(0, self._snap_pixel_to_constraint(mask_start_pixel, frame_constraint, "start"))
            mask_end_pixel = min(
                self._snap_pixel_to_constraint(mask_end_pixel, frame_constraint, "end"),
                source_frame_count + frame_count_padding,
            )
            mask_start_time = mask_start_pixel / proj_fps if proj_fps > 0 else 0.0
            mask_end_time = mask_end_pixel / proj_fps if proj_fps > 0 else 0.0

            # --- Render composited frames ---
            render_started_at = time.perf_counter()
            logger.info("render start: scene_id=%s range=%d-%d", scene.scene_id, render_start, render_end)
            rendered_frames = self._render_scene_frames(proj, scene, render_start, render_end)
            if frame_count_padding > 0:
                rendered_frames = self._pad_image_batch_to_frame_count(rendered_frames, target_frame_count)
                frame_count = target_frame_count
            logger.info(
                "render end: scene_id=%s frames=%d duration=%.2fs",
                scene.scene_id,
                frame_count,
                time.perf_counter() - render_started_at,
            )

            # --- Gather guide frames within render range ---
            guide_images = []
            guide_indices = []
            guide_strengths = []
            guide_frames = scene.guide_frames
            if queue_job and snapshot_version > 0:
                guide_frames = [
                    GuideFrame.from_dict(guide)
                    for guide in getattr(queue_job, "guide_frame_snapshots", [])
                    if isinstance(guide, dict)
                ]
            elif getattr(getattr(scene, "guide_track_config", None), "hidden", False):
                guide_frames = []

            for guide in guide_frames:
                if getattr(guide, "muted", False):
                    continue
                idx = guide.frame_index
                if idx == -1:
                    idx = scene.duration_frames - 1

                if render_start <= idx < render_end:
                    asset = proj.get_asset(guide.asset_id)
                    if asset:
                        asset_path = resolve_existing_project_path(
                            proj,
                            asset.path,
                            purpose=f"guide asset {getattr(asset, 'asset_id', '') or '(unknown)'}",
                        )
                        if os.path.isfile(asset_path):
                            img = self._load_guide_image(
                                asset_path, asset.asset_type, proj_w, proj_h,
                                fit_mode=getattr(guide, "fit_mode", "pad_edge"),
                                crop_position=getattr(guide, "crop_position", "center"),
                            )
                            if img is not None:
                                guide_images.append(img)
                                local_idx = idx - render_start
                                guide_indices.append(str(local_idx))
                                guide_strengths.append(f"{getattr(guide, 'strength', 1.0):.4f}")

            if guide_images:
                guide_tensor = torch.stack(guide_images, dim=0)
                indices_str = ",".join(guide_indices)
                strengths_str = ",".join(guide_strengths)
            else:
                guide_tensor = torch.zeros(1, proj_h, proj_w, 3, dtype=torch.float32)
                indices_str = ""
                strengths_str = ""

            # --- Get prompt for render range ---
            # Channel-label + delimiter composition honors the project-durable
            # knobs; queued jobs use their frozen composed prompt instead.
            prompt_labels_on = True
            prompt_delimiter = "."
            prompt_threshold = 0.0
            proj_metadata = getattr(proj, "metadata", None)
            if isinstance(proj_metadata, dict):
                prompt_labels_on = proj_metadata.get("prompt_channel_labels", True) is not False
                prompt_delimiter = str(proj_metadata.get("prompt_section_delimiter", ".") or "")
                try:
                    prompt_threshold = float(proj_metadata.get("prompt_frame_threshold", 0.0) or 0.0)
                except (TypeError, ValueError):
                    prompt_threshold = 0.0
            if queue_job:
                prompt_text = getattr(queue_job, "prompt", "")
                if snapshot_version <= 0 and not prompt_text:
                    prompt_text = scene.get_prompt_for_range(
                        render_start, render_end,
                        labels_on=prompt_labels_on, delimiter=prompt_delimiter,
                        boundary_threshold_pct=prompt_threshold)
            else:
                prompt_text = scene.get_prompt_for_range(
                    render_start, render_end,
                    labels_on=prompt_labels_on, delimiter=prompt_delimiter,
                    boundary_threshold_pct=prompt_threshold)

            # --- Load audio from scene's audio tracks for the render range ---
            audio = self._load_scene_audio(proj, scene, render_start, render_end)
            if frame_count_padding > 0:
                audio = self._pad_audio_to_frame_count(audio, frame_count, proj_fps)

            # --- Queue snapshots freeze take placement MODE only; linked/muted are
            # live editing preferences resolved from the hidden widgets at execution
            # (user decision 2026-06-11 — never frozen per job). ---
            if queue_job:
                raw_mode = getattr(queue_job, "take_placement_mode", None)
                if raw_mode in ("trimmed", "untrimmed"):
                    take_placement_mode = raw_mode

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
                "actual_pre_context_frames": actual_pre,
                "actual_post_context_frames": actual_post,
                "mask_pre_offset": mask_pre_offset,
                "mask_post_offset": mask_post_offset,
                "mask_start_frame": mask_start_pixel,
                "mask_end_frame": mask_end_pixel,
                "template_id": template_id,
                "frame_constraint": frame_constraint,
                "source_frame_count": source_frame_count,
                "frame_count": frame_count,
                "frame_count_padding": frame_count_padding,
                "take_placement_mode": take_placement_mode,
                "take_placement_linked": take_placement_linked,
                "take_placement_muted": take_placement_muted,
                "prompt": prompt_text,
                # Consume-only completion handle for save nodes — do NOT set on peek
                "queue_job_id": queue_job.job_id if queue_job and queue_job_consumed else "",
                # Snapshot reference for read-only consumers (prompt relay bridge):
                # set on BOTH peek and consume. Peek runs (queue-active, no Sonder
                # terminal save) are the mainline relay workflow; keying off the
                # consume-only handle would silently resolve LIVE prompt state.
                "queue_job_ref_id": queue_job.job_id if queue_job else "",
                "base_modified_at": execution_base_modified_at,
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
                strengths_str,
                prompt_text,
                frame_count,
                proj_fps,
                proj_w,
                proj_h,
                audio,
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
            if proj is not None and queue_job is not None and queue_job_consumed:
                self._mark_queue_job_failed(proj, queue_job, str(e))
            raise

    def _render_scene_frames(self, proj: TimelineProject, scene: Scene,
                              render_start: int, render_end: int) -> torch.Tensor:
        """Composite all visible video clips into frames for the given range.

        Returns (N, H, W, 3) float32 RGB tensor. Uses caching to skip
        re-rendering when the scene hasn't changed.
        """
        return render_scene_frames(
            proj,
            scene,
            render_start,
            render_end,
            video_capture_factory=cv2.VideoCapture,
        )

    @staticmethod
    def _fit_frame_to_canvas(frame_bgr: np.ndarray, canvas_w: int, canvas_h: int,
                             mode: str = "pad_edge", crop_position: str = "center"):
        """Resize frame to fit canvas per fit mode (letterbox/edge-pad/cover/stretch).

        Returns:
            tuple: (canvas, (x_off, y_off, new_w, new_h)) — placed frame and content bounds.
        """
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        placed_rgb, bounds = fit_frame_to_canvas(frame_rgb, canvas_w, canvas_h,
                                                 mode=mode, crop_position=crop_position)
        return cv2.cvtColor(placed_rgb, cv2.COLOR_RGB2BGR), bounds

    def _load_guide_image(self, path: str, asset_type: str,
                          target_w: int, target_h: int,
                          fit_mode: str = "pad_edge", crop_position: str = "center") -> torch.Tensor | None:
        """Load an image file and return as (H, W, 3) float32 RGB tensor."""
        try:
            if asset_type == "video":
                frame_rgb = decode_video_frame(path, 0)
                if frame_rgb is None:
                    cap = cv2.VideoCapture(path)
                    try:
                        if not cap.isOpened():
                            return None
                        ret, frame_bgr = cap.read()
                        if not ret:
                            return None
                        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
                    finally:
                        cap.release()
            else:
                # Load image
                frame_bgr = cv2.imread(path, cv2.IMREAD_COLOR)
                if frame_bgr is None:
                    return None
                frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

            rgb, _bounds = fit_frame_to_canvas(frame_rgb, target_w, target_h,
                                               mode=fit_mode, crop_position=crop_position)

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
        # Scene-level fps override
        effective_fps = scene.fps if hasattr(scene, 'fps') and scene.fps > 0 else proj.fps
        duration_sec = (sel_end - sel_start) / effective_fps if effective_fps > 0 else 1.0
        sample_rate = 44100

        if not scene.audio_tracks:
            return _make_silent_audio(duration_sec, sample_rate)

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
        failed_tracks = 0
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
            overlap_start = max(sel_start, int(track.timeline_start_frame or 0))
            overlap_end = min(sel_end, int(track.timeline_end_frame or 0))
            if overlap_end <= overlap_start:
                logger.debug(
                    "Skipping scene audio track %s: no overlap with range %d-%d",
                    track.source_path,
                    sel_start,
                    sel_end,
                )
                continue

            raw_path = track.source_path
            src_path = resolve_existing_project_path(
                proj,
                raw_path,
                purpose="scene audio track",
            )
            if not os.path.isfile(src_path):
                logger.info(
                    "Skipping scene audio track %s: file not found or quarantined",
                    track.source_path,
                )
                continue

            try:
                samples, _sr = decode_audio_samples(
                    src_path,
                    sample_rate=sample_rate,
                    channels=2,
                    mix_to_mono=False,
                )
                waveform = torch.from_numpy(np.ascontiguousarray(samples, dtype=np.float32))

                # Calculate the overlapping source slice and destination offset.
                track_offset_frames = overlap_start - sel_start
                audio_offset_frames = overlap_start - int(track.timeline_start_frame or 0)
                overlap_frames = overlap_end - overlap_start

                track_offset_samples = int(track_offset_frames / effective_fps * sample_rate)
                # BUG-3 fix: include source_in_frame for trimmed/split audio tracks
                source_offset_frames = int(getattr(track, "source_in_frame", 0) or 0) + audio_offset_frames
                audio_offset_samples = int(source_offset_frames / effective_fps * sample_rate)
                overlap_samples = int(overlap_frames / effective_fps * sample_rate)

                # Trim source audio
                src_audio = waveform[:, audio_offset_samples:audio_offset_samples + overlap_samples]
                available = min(overlap_samples, total_samples - track_offset_samples)
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
            except Exception as e:
                failed_tracks += 1
                logger.warning("Failed to decode/mix scene audio track %s: %s", track.source_path, e)
                continue

        if not any_loaded:
            logger.info(
                "Scene audio fell back to silence: considered=%d loaded=%d failed=%d range=%d-%d",
                considered_tracks,
                loaded_tracks,
                failed_tracks,
                sel_start,
                sel_end,
            )
            return _make_silent_audio(duration_sec, sample_rate)

        # Clamp to prevent clipping
        mixed = mixed.clamp(-1.0, 1.0)
        return {"waveform": mixed.unsqueeze(0), "sample_rate": sample_rate}
