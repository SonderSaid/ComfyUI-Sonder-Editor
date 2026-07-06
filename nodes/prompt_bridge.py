"""Sonder Prompt Relay Bridge — exports the active render window's prompt
state as a kijai ComfyUI-PromptRelay payload.

Single adapter node (no Start/End loop): wire the editor's `project` output
in, then wire the string outputs into `Prompt Relay Encode (Smart)`
(global_prompt + smart_prompt) or the manual `Prompt Relay Encode`
(global_prompt + local_prompts + segment_lengths). No model patching happens
here — PromptRelay (Wan/LTX only; conflicts with other attention patchers)
owns the temporal routing, and the input latent remains execution truth for
frame counts.

Snapshot-vs-live resolution mirrors the guides bridge, with one deliberate
difference: jobs resolve via the ctx key `queue_job_ref_id`, which the editor
sets on BOTH peek and consume. The consume-only `queue_job_id` is the save
nodes' completion handle — keying off it would silently resolve LIVE prompt
state on peek runs (queue-active with a third-party save), which are the
mainline relay workflow.
"""

from __future__ import annotations

import logging

from ..server import prompt_payload

logger = logging.getLogger(__name__)


def _resolve_active_scene(project):
    ctx = getattr(project, "_execution_context", None) or {}
    scene_id = ctx.get("scene_id", "")
    if scene_id:
        scene = project.get_scene(scene_id)
        if scene is not None:
            return scene
    scenes = getattr(project, "scenes", None) or []
    return scenes[0] if scenes else None


def _resolve_prompt_window(project, scene):
    ctx = getattr(project, "_execution_context", None) or {}
    window_start = ctx.get("context_start")
    window_end = ctx.get("context_end")
    if window_start is None or window_end is None:
        window_start = 0
        window_end = getattr(scene, "duration_frames", 0) if scene is not None else 0
    return int(window_start), int(window_end)


def _find_ref_job(project):
    """The pending/running job this execution rendered (peek OR consume)."""
    ctx = getattr(project, "_execution_context", None) or {}
    job_id = ctx.get("queue_job_ref_id", "") or ""
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
    try:
        return max(0, int(params.get("snapshot_version", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _project_labels_on(project) -> bool:
    metadata = getattr(project, "metadata", None)
    if isinstance(metadata, dict):
        return metadata.get("prompt_channel_labels", False) is True
    return False


def _threshold_from(source, default: float = 10.0) -> float:
    if not isinstance(source, dict):
        return default
    try:
        return float(source.get("prompt_frame_threshold", default) or 0.0)
    except (TypeError, ValueError):
        return default


def resolve_window_prompt_state(project):
    """Resolve (global_text, sections, labels_on, window, source, threshold).

    Frozen job snapshot when this execution rendered a snapshot_version>0
    job (its hidden flags + threshold were baked at enqueue); live scene +
    project metadata + per-lane hidden flags otherwise.
    """
    scene = _resolve_active_scene(project)
    window_start, window_end = _resolve_prompt_window(project, scene)

    queue_job = _find_ref_job(project)
    if queue_job is not None and _snapshot_version(queue_job) > 0:
        params = getattr(queue_job, "params", {}) or {}
        labels_on = params.get("prompt_channel_labels", False) is True \
            if isinstance(params, dict) else False
        return (
            str(getattr(queue_job, "scene_prompt", "") or ""),
            list(getattr(queue_job, "prompt_sections", []) or []),
            labels_on,
            window_start,
            window_end,
            "snapshot",
            _threshold_from(params),
        )

    labels_on = _project_labels_on(project)
    threshold = _threshold_from(getattr(project, "metadata", None))
    if scene is None:
        return "", [], labels_on, window_start, window_end, "live", threshold
    global_hidden = bool(getattr(getattr(scene, "global_prompt_track_config", None), "hidden", False))
    sections_hidden = bool(getattr(getattr(scene, "prompt_track_config", None), "hidden", False))
    global_text = "" if global_hidden else str(getattr(scene, "prompt", "") or "")
    sections = [] if sections_hidden else list(getattr(scene, "prompt_sections", []) or [])
    return global_text, sections, labels_on, window_start, window_end, "live", threshold


def build_window_relay_payload(project) -> dict:
    """Window-resolved PromptRelay payload (the bridge's testable core)."""
    global_text, sections, labels_on, window_start, window_end, source, threshold = (
        resolve_window_prompt_state(project)
    )
    segments = prompt_payload.resolve_segments(
        sections, window_start, window_end, labels_on, threshold)
    payload = prompt_payload.build_relay_payload(global_text, segments)
    payload["labels_on"] = labels_on
    payload["window_start"] = window_start
    payload["window_end"] = window_end
    payload["source"] = source
    return payload


class SonderPromptRelayBridge:
    """Export the render window's prompts as PromptRelay-ready strings."""

    CATEGORY = "Sonder"
    RETURN_TYPES = ("STRING", "STRING", "STRING", "STRING")
    RETURN_NAMES = ("global_prompt", "smart_prompt", "local_prompts", "segment_lengths")
    OUTPUT_TOOLTIPS = (
        "Scene-global prompt text (always-on identity/style). Passed through "
        "untouched. If empty, PromptRelay's Smart node auto-promotes the first "
        "local segment to global (and keeps it local) — documented passthrough.",
        "Pipe-separated local prompts with [start-end] tags in window-rebased "
        "REAL frame numbers (only spans matter to the parser, so real frames "
        "are proportionally exact and match the editor). Wire into Prompt "
        "Relay Encode (Smart) `smart_prompt`.",
        "Pipe-separated local prompt texts without tags. Wire into the manual "
        "Prompt Relay Encode `local_prompts` together with segment_lengths.",
        "Comma-separated pixel-frame segment lengths for the manual Prompt "
        "Relay Encode `segment_lengths`. Tail frame padding the editor adds "
        "for template constraints inherits the last segment's prompt.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Adapter for kijai's ComfyUI-PromptRelay (Wan/LTX families): converts the "
        "Sonder editor's global + segment prompt lanes for the active render window "
        "into relay payload strings. Sections hold until the next section starts; "
        "segment text is sanitized (| -> ,, newlines flattened, numeric [n]/[n-m] "
        "tags stripped) so the relay parsers cannot misread it. Queued runs use the "
        "frozen job snapshot; live runs read the scene. PromptRelay conflicts with "
        "other attention patchers and reads frame counts from the sampled latent."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project": ("SONDER_PROJECT", {
                    "tooltip": "Wire from the Sonder Editor's project output so the "
                               "bridge sees the same execution window.",
                }),
            },
        }

    def execute(self, project):
        payload = build_window_relay_payload(project)
        # Provenance: public (non-underscore) ctx keys auto-flow into take/asset
        # generation_params via _public_execution_context at save time.
        ctx = getattr(project, "_execution_context", None)
        if isinstance(ctx, dict):
            ctx["prompt_relay"] = {
                "global": payload["global_prompt"],
                "segments": [
                    {"text": s["text"], "start": s["start"], "end": s["end"]}
                    for s in payload.get("segments", [])
                ],
                "labels": payload["labels_on"],
                "source": payload["source"],
            }
        logger.info(
            "prompt relay bridge: source=%s window=[%d,%d) segments=%d",
            payload["source"], payload["window_start"], payload["window_end"],
            len(payload.get("segments", [])),
        )
        return (
            payload["global_prompt"],
            payload["smart_prompt"],
            payload["local_prompts"],
            payload["segment_lengths"],
        )
