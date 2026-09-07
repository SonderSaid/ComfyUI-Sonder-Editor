"""Frozen prompt-envelope versioning and the released v0.2.2 composer.

The marker-absent branch is a deliberate public compatibility path.  It is
kept isolated from Prompt Context so queued v0.2.2 jobs cannot accidentally
start consulting live scene state or newly-added compiler authorities.

Expiry: remove the marker-absent branch only in a deliberate breaking release
that drops the published v0.2.2 queue contract.
"""

from __future__ import annotations

from . import prompt_payload


PROMPT_CONTEXT_FORMAT = "prompt_context_v1"

# Reviewed directly against origin/main v0.2.2 `_queue_job_from_body` and the
# server-owned params it added before persistence.  Marker-absent requests do
# not gain new meaning merely because the current dataclass knows more fields.
V022_QUEUE_BODY_FIELDS = frozenset({
    "scene_id", "scene_name", "selection_start", "selection_end",
    "batch_id", "batch_total", "batch_index", "prompt", "scene_prompt",
    "context_frames", "pre_context_frames", "post_context_frames",
    "mask_pre_offset", "mask_post_offset", "guide_frame_snapshots",
    "driver_clip_snapshots", "driver_lane_count", "driver_lane_configs",
    "prompt_sections", "scene_width", "scene_height", "scene_fps",
    "template_id", "frame_constraint", "take_placement_mode", "params",
})
V022_PARAM_FIELDS = frozenset({
    "snapshot_version", "prompt_channel_labels", "prompt_section_delimiter",
    "prompt_frame_threshold", "guide_collision_auto_offset",
    "guide_collision_prediction",
})

# Fields added after the v0.2.2 GenerationJob contract.  Default/empty values
# are harmless because current deserialization materializes them on every job;
# a marker-absent envelope becomes ambiguous only when one carries state.
POST_V022_JOB_DEFAULTS = {
    "reference_item_snapshots": [],
    "reference_lane_count": 1,
    "reference_lane_configs": [],
    "reference_lane_recipes": [],
    "compiled_prompt_context": {},
    "reference_input_snapshots": [],
    "minimax_h3_setup_snapshot": {},
    "dimension_constraint": None,
}

POST_V022_PARAM_FIELDS = {
    "scene_global_channels",
    "scene_global_channel_docs",
    "scene_global_attachments",
    "prompt_channel_template",
    "prompt_execution_window",
    "prompt_effective_fps",
    "prompt_context_profile_hash",
    "prompt_context_content_hash",
    "prompt_context_profile_id",
    "prompt_context_profile_config",
    "reference_frame_threshold",
}

POST_V022_SECTION_DEFAULTS = {
    "channel_docs": {},
    "attachments": [],
    "starts_new_shot": False,
    "shot_timestamp": False,
    "global_channel_exceptions": [],
}


class FrozenPromptEnvelopeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _differs_from_default(value, default) -> bool:
    if default is None:
        return value is not None
    if isinstance(default, (list, dict)):
        return bool(value)
    return value != default


def prompt_context_format(job) -> str | None:
    params = getattr(job, "params", {}) or {}
    if not isinstance(params, dict):
        return None
    raw = params.get("prompt_context_format")
    if raw is None or raw == "":
        return None
    value = str(raw)
    if value != PROMPT_CONTEXT_FORMAT:
        raise FrozenPromptEnvelopeError(
            "unsupported_prompt_context_format",
            f"Unsupported frozen prompt format: {value}",
        )
    return value


def validate_marker_absent_queue_body(body, params) -> None:
    """Enforce the reviewed v0.2.2 request envelope before fields are dropped."""
    if not isinstance(body, dict):
        raise FrozenPromptEnvelopeError(
            "unmarked_prompt_context_envelope", "Queue job must be an object")
    unknown = sorted(set(body).difference(V022_QUEUE_BODY_FIELDS))
    param_unknown = sorted(set(params or {}).difference(V022_PARAM_FIELDS)) \
        if isinstance(params, dict) else []
    if unknown or param_unknown:
        detail = ", ".join([*unknown, *(f"params.{key}" for key in param_unknown)])
        raise FrozenPromptEnvelopeError(
            "unmarked_prompt_context_envelope",
            f"A marker-absent queue request is outside the v0.2.2 allowlist: {detail}",
        )


def validate_marker_absent_v022(job) -> None:
    """Refuse marker-absent envelopes that contain unpublished authorities."""
    ambiguous = []
    for field, default in POST_V022_JOB_DEFAULTS.items():
        if _differs_from_default(getattr(job, field, default), default):
            ambiguous.append(field)

    params = getattr(job, "params", {}) or {}
    if isinstance(params, dict):
        ambiguous.extend(
            f"params.{field}" for field in sorted(set(params).difference(
                V022_PARAM_FIELDS))
        )
        ambiguous.extend(
            field for field in sorted(POST_V022_PARAM_FIELDS)
            if field in params and bool(params.get(field))
        )

    for index, section in enumerate(getattr(job, "prompt_sections", []) or []):
        if not isinstance(section, dict):
            continue
        for field, default in POST_V022_SECTION_DEFAULTS.items():
            if field in section and _differs_from_default(section.get(field), default):
                ambiguous.append(f"prompt_sections[{index}].{field}")

    if ambiguous:
        detail = ", ".join(ambiguous)
        raise FrozenPromptEnvelopeError(
            "unmarked_prompt_context_envelope",
            f"A marker-absent v0.2.2 job contains unpublished prompt state: {detail}",
        )


def classify_frozen_prompt(job) -> str:
    if getattr(job, "_frozen_unhydrated", False):
        raise FrozenPromptEnvelopeError("queue_snapshot_unhydrated", "Queue snapshot must be hydrated before prompt classification")
    marker = prompt_context_format(job)
    if marker is None:
        validate_marker_absent_v022(job)
        return "v0.2.2"
    return marker


def _snapshot_version(job) -> int:
    params = getattr(job, "params", {}) or {}
    if not isinstance(params, dict):
        return 0
    try:
        return max(0, int(params.get("snapshot_version", 0) or 0))
    except (TypeError, ValueError):
        return 0


def compose_v022_job_prompt(project, job) -> None:
    """Exact marker-absent v0.2.2 enqueue composition behavior."""
    if _snapshot_version(job) <= 0:
        return
    validate_marker_absent_v022(job)
    params = getattr(job, "params", {}) or {}
    metadata = getattr(project, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    labels_on = params.get(
        "prompt_channel_labels", metadata.get("prompt_channel_labels", False)
    ) is True
    params["prompt_channel_labels"] = labels_on
    delimiter = str(params.get(
        "prompt_section_delimiter",
        metadata.get("prompt_section_delimiter", prompt_payload.DEFAULT_SECTION_DELIMITER),
    ) or "")
    params["prompt_section_delimiter"] = delimiter
    try:
        threshold = float(params.get(
            "prompt_frame_threshold", metadata.get("prompt_frame_threshold", 10.0)
        ) or 0.0)
    except (TypeError, ValueError):
        threshold = 10.0
    params["prompt_frame_threshold"] = threshold
    job.params = params
    window_start = max(
        0,
        int(getattr(job, "selection_start", 0) or 0)
        - int(getattr(job, "pre_context_frames", 0) or 0),
    )
    window_end = (
        int(getattr(job, "selection_end", 0) or 0)
        + int(getattr(job, "post_context_frames", 0) or 0)
    )
    job.prompt = prompt_payload.compose_range_prompt(
        getattr(job, "scene_prompt", "") or "",
        getattr(job, "prompt_sections", []) or [],
        window_start,
        window_end,
        labels_on=labels_on,
        delimiter=delimiter,
        boundary_threshold_pct=threshold,
    )


def build_v022_relay_payload(job, window_start: int, window_end: int) -> dict:
    """Build released Prompt Relay output from frozen v0.2.2 fields only."""
    validate_marker_absent_v022(job)
    params = getattr(job, "params", {}) or {}
    labels_on = params.get("prompt_channel_labels", False) is True \
        if isinstance(params, dict) else False
    try:
        threshold = float(params.get("prompt_frame_threshold", 10.0) or 0.0)
    except (TypeError, ValueError):
        threshold = 10.0
    segments = prompt_payload.resolve_segments(
        list(getattr(job, "prompt_sections", []) or []),
        int(window_start),
        int(window_end),
        labels_on,
        threshold,
    )
    payload = prompt_payload.build_relay_payload(
        str(getattr(job, "scene_prompt", "") or ""), segments
    )
    payload.update({
        "labels_on": labels_on,
        "window_start": int(window_start),
        "window_end": int(window_end),
        "source": "snapshot",
    })
    return payload
