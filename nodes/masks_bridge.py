"""Sonder Masks Bridge — per-channel video/audio generation-mask gating.

The Sonder Editor emits a single generation-mask window (`mask_start_time` /
`mask_end_time`, in seconds) describing which part of the render range is
generated; the pre/post context frames outside it are kept/conditioning.

This bridge exposes that one window as four mask-time outputs — separate
video and audio pairs — each gated by an Edit/Freeze toggle. Freezing a
channel collapses its window to zero width at `mask_start_time`, so nothing
is generated for it (the channel is kept from source and can drive the
other). This removes the friction of manually plugging/unplugging the
downstream temporal-mask nodes.

It is project-only and read-only, a sibling of `SonderDriverBridge` /
`SonderPromptRelayBridge`. It reads the resolved mask frames from
`project._execution_context`, which the editor already computes identically
for live and queued renders, so no queue-snapshot resolution is needed for
the region. FPS is resolved the way the editor does (frozen job `scene_fps`
for snapshot jobs, else live `scene.fps`, else `project.fps`) so the seconds
reproduce the editor's outputs exactly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_active_scene(project):
    ctx = getattr(project, "_execution_context", None) or {}
    scene_id = ctx.get("scene_id", "")
    if scene_id:
        scene = project.get_scene(scene_id)
        if scene is not None:
            return scene
    scenes = getattr(project, "scenes", None) or []
    return scenes[0] if scenes else None


def _find_ref_job(project):
    """The pending/running job this execution rendered (peek OR consume)."""
    ctx = getattr(project, "_execution_context", None) or {}
    job_id = str(ctx.get("queue_job_ref_id", "") or "")
    if not job_id:
        return None
    for job in getattr(project, "generation_queue", []) or []:
        if getattr(job, "job_id", "") == job_id:
            return job
    return None


def _snapshot_version(queue_job) -> int:
    params = getattr(queue_job, "params", {}) or {}
    if not isinstance(params, dict):
        return 0
    return max(0, _coerce_int(params.get("snapshot_version", 0), 0))


def _resolve_fps(project) -> float:
    """Mirror SonderEditor's fps resolution so seconds match slots 15-16.

    Frozen job `scene_fps` (snapshot jobs) wins, else the live scene fps
    override, else the project fps.
    """
    job = _find_ref_job(project)
    if job is not None and _snapshot_version(job) > 0:
        fps = _coerce_float(getattr(job, "scene_fps", 0.0), 0.0)
        if fps > 0:
            return fps
        return _coerce_float(getattr(project, "fps", 0.0), 0.0)
    scene = _resolve_active_scene(project)
    if scene is not None:
        fps = _coerce_float(getattr(scene, "fps", 0.0), 0.0)
        if fps > 0:
            return fps
    return _coerce_float(getattr(project, "fps", 0.0), 0.0)


def resolve_mask_times(project, edit_video: bool = True, edit_audio: bool = True) -> dict:
    """Gate the editor's generation-mask window per channel (the testable core).

    Raises RuntimeError when the wired project carries no render context, so a
    misconfigured graph fails loud rather than emitting a silent zero window.
    """
    ctx = getattr(project, "_execution_context", None)
    if not isinstance(ctx, dict) or "mask_start_frame" not in ctx or "mask_end_frame" not in ctx:
        raise RuntimeError(
            "SonderMasksBridge: no Sonder render context found on the project. "
            "Wire this node downstream of a Sonder Editor that executed in this prompt."
        )

    fps = _resolve_fps(project)
    start_frame = _coerce_int(ctx.get("mask_start_frame"), 0)
    end_frame = _coerce_int(ctx.get("mask_end_frame"), 0)

    def to_sec(frame: int) -> float:
        return (frame / fps) if fps > 0 else 0.0

    full_start = to_sec(start_frame)
    full_end = to_sec(end_frame)

    video = (full_start, full_end) if edit_video else (full_start, full_start)
    audio = (full_start, full_end) if edit_audio else (full_start, full_start)

    return {
        "video_mask_start_time": video[0],
        "video_mask_end_time": video[1],
        "audio_mask_start_time": audio[0],
        "audio_mask_end_time": audio[1],
        "edit_video": bool(edit_video),
        "edit_audio": bool(edit_audio),
    }


class SonderMasksBridge:
    """Gate the editor's generation-mask window into per-channel mask times."""

    CATEGORY = "Sonder"
    RETURN_TYPES = ("FLOAT", "FLOAT", "FLOAT", "FLOAT")
    RETURN_NAMES = (
        "video_mask_start_time",
        "video_mask_end_time",
        "audio_mask_start_time",
        "audio_mask_end_time",
    )
    OUTPUT_TOOLTIPS = (
        "Video generation-mask start time (seconds). Equals the editor's mask "
        "start when Edit Video is on; equals the end (zero width) when frozen.",
        "Video generation-mask end time (seconds). Collapses to the start time "
        "when Edit Video is off, freezing the video channel.",
        "Audio generation-mask start time (seconds). Equals the editor's mask "
        "start when Edit Audio is on; equals the end (zero width) when frozen.",
        "Audio generation-mask end time (seconds). Collapses to the start time "
        "when Edit Audio is off, freezing the audio channel.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Reads the Sonder Editor's generation-mask window and exposes it as "
        "separate video and audio mask-time pairs, each gated by an Edit/Freeze "
        "toggle. A frozen channel emits a zero-width window (start == end) so "
        "nothing is generated for it — useful to keep audio fixed while it drives "
        "video, or vice versa. Wire the outputs into a downstream temporal mask "
        "node. Requires a project that executed through a Sonder Editor."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project": ("SONDER_PROJECT", {
                    "tooltip": "Wire from the Sonder Editor project output so the "
                               "bridge sees the same execution window.",
                }),
                "edit_video": ("BOOLEAN", {
                    "default": True,
                    "label_on": "Edit",
                    "label_off": "Freeze",
                    "tooltip": "Edit: video generates over the mask window. "
                               "Freeze: zero-width window keeps video from source.",
                }),
                "edit_audio": ("BOOLEAN", {
                    "default": True,
                    "label_on": "Edit",
                    "label_off": "Freeze",
                    "tooltip": "Edit: audio generates over the mask window. "
                               "Freeze: zero-width window keeps audio from source.",
                }),
            },
        }

    def execute(self, project, edit_video=True, edit_audio=True):
        result = resolve_mask_times(project, edit_video, edit_audio)
        # Provenance: public (non-underscore) ctx keys auto-flow into
        # take/asset generation_params via _public_execution_context at save time.
        ctx = getattr(project, "_execution_context", None)
        if isinstance(ctx, dict):
            ctx["masks_bridge"] = {
                "edit_video": result["edit_video"],
                "edit_audio": result["edit_audio"],
                "video_mask": [result["video_mask_start_time"], result["video_mask_end_time"]],
                "audio_mask": [result["audio_mask_start_time"], result["audio_mask_end_time"]],
            }
        logger.info(
            "masks bridge: edit_video=%s edit_audio=%s video=[%.4f,%.4f] audio=[%.4f,%.4f]",
            result["edit_video"],
            result["edit_audio"],
            result["video_mask_start_time"],
            result["video_mask_end_time"],
            result["audio_mask_start_time"],
            result["audio_mask_end_time"],
        )
        return (
            result["video_mask_start_time"],
            result["video_mask_end_time"],
            result["audio_mask_start_time"],
            result["audio_mask_end_time"],
        )
