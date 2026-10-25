"""The Sonder Editor's optional execution inputs: one spec for the node and its hosts.

``SonderEditor.INPUT_TYPES`` reads ``editor_optional_inputs()`` for its optional
widgets, and the SDK host validates the editor values it stores with
``clean_editor_values``, so the two cannot drift. Order is workflow API: the
frontend maps saved ``widgets_values`` by position, so new inputs are appended.
"""

from typing import Any

from .media_helpers import CROP_POSITIONS, DEFAULT_CROP_POSITION, DEFAULT_FIT_MODE, FIT_MODES

TAKE_PLACEMENT_MODES = ("trimmed", "untrimmed")
MAX_SAFE_INTEGER = 9_007_199_254_740_991
_STRING_CAP = 256


def editor_optional_inputs() -> dict[str, tuple[str, dict[str, Any]]]:
    """A fresh copy of the editor's optional inputs, in workflow order."""
    return {
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
        "render_cache_max_bytes": ("INT", {
            "default": 0, "min": -1, "max": MAX_SAFE_INTEGER,
            "tooltip": "Project render-cache budget in bytes. 0 disables caching; -1 is unlimited.",
        }),
        "render_queue_active": ("BOOLEAN", {
            "default": True,
            "tooltip": "When enabled, queue jobs drive editor execution. Disable to render the live selection without consuming queue jobs.",
        }),
        # Append new hidden widgets so existing workflow widget_values keep
        # their established positional mapping.
        "take_fit_mode": ("STRING", {
            "default": DEFAULT_FIT_MODE,
            "tooltip": "Fit mode stamped on newly placed takes (set by editor settings).",
        }),
        "take_crop_position": ("STRING", {
            "default": DEFAULT_CROP_POSITION,
            "tooltip": "Crop anchor stamped on newly placed takes (set by editor settings).",
        }),
        "take_keep_padding": ("BOOLEAN", {
            "default": False,
            "tooltip": "Place a take with its generated model-frame padding instead of trimming it (set by editor settings).",
        }),
    }


# The string inputs whose value must be one of a fixed set.
_STRING_CHOICES = {
    "take_placement_mode": TAKE_PLACEMENT_MODES,
    "take_fit_mode": FIT_MODES,
    "take_crop_position": CROP_POSITIONS,
}


def clean_editor_value(name: str, value: Any) -> tuple[bool, Any]:
    """``(True, value)`` when ``value`` is valid for input ``name``, else ``(False, None)``."""
    spec = editor_optional_inputs().get(name)
    if spec is None:
        return False, None
    kind, options = spec
    if kind == "BOOLEAN":
        return (True, value) if isinstance(value, bool) else (False, None)
    if kind == "INT":
        if isinstance(value, bool) or not isinstance(value, int):
            return False, None
        if value < options.get("min", value) or value > options.get("max", value):
            return False, None
        return True, value
    if not isinstance(value, str) or len(value) > _STRING_CAP:
        return False, None
    choices = _STRING_CHOICES.get(name)
    if choices is not None and value not in choices:
        return False, None
    return True, value


def clean_editor_values(values: Any, names) -> dict[str, Any]:
    """The valid entries of ``values`` among ``names``; anything else is dropped."""
    if not isinstance(values, dict):
        return {}
    cleaned = {}
    for name in names:
        if name in values:
            ok, value = clean_editor_value(name, values[name])
            if ok:
                cleaned[name] = value
    return cleaned
