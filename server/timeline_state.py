import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import datetime
import os
from typing import Any

from . import prompt_payload
from .lane_registry import VARIABLE_LANE_DESCRIPTORS, pad_lane_configs, pad_lane_recipes
from .reference_resolution import REFERENCE_OUTPUT_NAMES, migrate_live_outputs

logger = logging.getLogger("sonder_editor")


# ---------------------------------------------------------------------------
# Asset registry — organized catalog of all project media
# ---------------------------------------------------------------------------

VIDEO_ASSET_EXTS = {".mp4", ".mov", ".webm", ".mkv"}
IMAGE_ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_ASSET_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".aac", ".m4a"}
ARTIFACT_KIND_BY_EXT = {
    ".latent": "latent",
    ".safetensors": "model",
    ".pt": "model",
    ".pth": "model",
    ".ckpt": "model",
    ".json": "json",
    ".txt": "text",
}

REFERENCE_KINDS = {"character", "location", "prop", "outfit"}
REFERENCE_CLASSES = {"subject", "context"}
REFERENCE_TAG_NAMESPACE = "sonder:"
REFERENCE_TAG_PRESETS = (
    {"id": "sonder:subject_still", "label": "Subject Still", "asset_types": ["image"], "suggested_kinds": ["character", "prop", "outfit"]},
    {"id": "sonder:subject_clip", "label": "Subject Clip", "asset_types": ["video"], "suggested_kinds": ["character", "prop", "outfit"]},
    {"id": "sonder:context_clip", "label": "Context Clip", "asset_types": ["video"], "suggested_kinds": ["location"]},
    {"id": "sonder:motion_reference", "label": "Motion Reference", "asset_types": ["video"], "suggested_kinds": ["character", "prop", "outfit"]},
    {"id": "sonder:portrait", "label": "Portrait", "asset_types": ["image", "video"], "suggested_kinds": ["character"]},
    {"id": "sonder:face_closeup", "label": "Face Close-up", "asset_types": ["image", "video"], "suggested_kinds": ["character"]},
    {"id": "sonder:full_body", "label": "Full Body", "asset_types": ["image", "video"], "suggested_kinds": ["character", "outfit"]},
    {"id": "sonder:turnaround", "label": "Turnaround", "asset_types": ["image", "video"], "suggested_kinds": ["character", "prop", "outfit"]},
    {"id": "sonder:character_sheet", "label": "Character Sheet", "asset_types": ["image"], "suggested_kinds": ["character", "outfit"]},
    {"id": "sonder:additional_view", "label": "Additional View", "asset_types": ["image", "video"], "suggested_kinds": ["character", "location", "prop", "outfit"]},
    {"id": "sonder:prop_angle", "label": "Prop Angle", "asset_types": ["image", "video"], "suggested_kinds": ["prop"]},
    {"id": "sonder:location", "label": "Location", "asset_types": ["image", "video"], "suggested_kinds": ["location"]},
    {"id": "sonder:first_frame", "label": "First Frame", "asset_types": ["image"], "suggested_kinds": ["location"]},
    {"id": "sonder:voice_identity", "label": "Voice Identity", "asset_types": ["audio", "video"], "suggested_kinds": ["character"], "requires_audio": True},
)

# Backend-owned recipe catalog. Lane state stores a materialized copy of the
# selected recipe so projects remain reproducible if this catalog evolves.
#
# `live_outputs` is the per-recipe Bridge output liveness map: every output NOT
# listed is dead for that mechanism and emits its documented type-correct
# fallback (empty IMAGE / silent AUDIO / 0). "slots" covers the whole r01..r16
# block. A recipe with no `live_outputs` (detached/custom) keeps every output
# live. Values are read from the surveyed node sources, not from model cards —
# see memory/Research Entries/reference_conditioning_mechanisms_2026-07.md.
REFERENCE_RECIPE_PRESETS = (
    {
        "id": "sonder:ltx_msr",
        # Surveyed LiconMSR uses contiguous 8x-grid segments from index 0 and a
        # separate `background` socket. Sonder deliberately overrides the latter
        # by sorting a context-class member to the temporal tail (user decision).
        "name": "LTX Multiple Subject Reference",
        "media_kind": "image",
        "hard": {"assembly": "temporal", "max_members": 5, "frame_step": 8, "frame_offset": 1,
                 "allowed_frame_counts": [17, 25, 33, 41, 49, 57, 65],
                 "live_outputs": ["image_slots", "reference_prompt", "reference_names"]},
        "soft": {"suggested_tags": ["sonder:subject_still"], "context_tag": "sonder:location"},
    },
    {
        "id": "sonder:ltx_ingredients",
        # One panel sheet at exact output resolution, looped as a static video.
        # 121 is the assembled reference-sequence length, never a render-window
        # constraint — the editor's {step, offset} governs the render window.
        "name": "LTX IC-LoRA Ingredients",
        "media_kind": "image",
        "hard": {"assembly": "sheet", "max_members": 16, "output_size": "scene", "background": "black",
                 "loop_frames": 121, "frame_step": 8, "frame_offset": 1,
                 "live_outputs": ["image_slots", "reference_prompt", "reference_names"]},
        "soft": {"suggested_tags": ["sonder:face_closeup", "sonder:full_body", "sonder:turnaround"],
                 "prompt_prefix": "Reference sheet:"},
    },
    {
        "id": "sonder:ltx_best_face_id",
        # A 4-panel sheet is exactly 1536x1024; a lone bust crop is ~460x406.
        "name": "LTX Best Face ID",
        "media_kind": "image",
        "hard": {"assembly": "sheet", "max_members": 4, "output_size": "custom", "width": 1536, "height": 1024,
                 "single_member_size": [460, 406], "background": "black",
                 "live_outputs": ["image_slots", "reference_prompt", "reference_names"]},
        "soft": {"suggested_tags": ["sonder:face_closeup"], "prompt_prefix": "ref_t2v:"},
    },
    {
        "id": "sonder:ltx_id_lora_audio",
        "name": "LTX ID-LoRA Voice Identity",
        "media_kind": "audio",
        "hard": {"assembly": "audio", "max_members": 16,
                 "live_outputs": ["audio_slots", "reference_prompt", "reference_names"]},
        "soft": {"suggested_tags": ["sonder:voice_identity"], "recommended_duration_sec": 5.0},
    },
    {
        "id": "sonder:wan_vace",
        # Native VACE takes reference_image[:1], so multi-reference requires one
        # composited sheet. The kijai wrapper is the reference implementation:
        # an equal-width vertical strip, padded to output aspect with white and
        # resized to width/height floored to /16.
        "name": "Wan VACE Reference Sheet",
        "media_kind": "image",
        "hard": {"assembly": "sheet", "layout": "strip", "max_members": 16,
                 "output_size": "scene", "size_multiple": 16, "background": "white",
                 "live_outputs": ["image_slots", "reference_prompt", "reference_names"]},
        "soft": {"silent_single_input": True},
    },
    {
        "id": "sonder:wan_phantom",
        "name": "Wan Phantom Identities",
        "media_kind": "image",
        "hard": {"assembly": "batch", "max_members": 4, "output_size": "scene",
                 "live_outputs": ["image_slots", "reference_prompt", "reference_names"]},
        "soft": {"suggested_tags": ["sonder:subject_still"]},
    },
    {
        "id": "sonder:wan_scail",
        "name": "Wan SCAIL Identities",
        "media_kind": "image",
        "hard": {"assembly": "batch", "max_members": 6, "output_size": "custom", "width": 512, "height": 896,
                 "primary_model_position": "last",
                 "live_outputs": ["image_slots", "reference_prompt", "reference_names"]},
        "soft": {"requires_identity_masks": True},
    },
    {
        "id": "sonder:wan_bernini",
        "name": "Wan Bernini-R References",
        "media_kind": "image",
        "hard": {"assembly": "slots", "max_members": 8, "output_size": "native",
                 "long_edge_max": 848, "size_multiple": 16,
                 "live_outputs": ["image_slots", "reference_prompt", "reference_names"]},
        "soft": {"prompt_tokens": "image{index}"},
    },
)


IMAGE_ASSEMBLIES = ("batch", "sheet", "temporal", "slots")

# Authoring schema for the values a recipe carries. This is the single
# declaration of what a recipe *has*: the Reference lane panel renders its form
# straight from it, custom-recipe validation checks against it, and every key
# here is read by the assembler in nodes/reference_core.py or drives a lane
# advisory. `applies_to` is the set of assembly modes a field is meaningful for
# (empty = all); `requires` names a sibling gate, matched against `requires_value`
# when that is set and against truthiness when it is empty.
REFERENCE_RECIPE_FIELDS = (
    {"key": "assembly", "section": "hard", "group": "Assembly", "label": "Assembly",
     "type": "enum", "values": ["batch", "sheet", "temporal", "slots", "audio"], "default": "batch",
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "How staged members become the numbered image-bridge outputs.",
     "value_help": {
         "batch": "Each member stays a separate image, stacked into one batch. The model receives them as a set of identities.",
         "sheet": "Members are composited into ONE image - a panel grid or a vertical strip - because the model only reads a single reference image.",
         "temporal": "Members are laid out along time, each holding a contiguous run of frames in the assembled sequence.",
         "slots": "Each member is emitted on its own numbered socket (r01, r02, ...) for models that take separately wired reference inputs.",
         "audio": "The staged member is trimmed audio rather than an image; only the audio outputs carry anything.",
     }},
    {"key": "layout", "section": "hard", "group": "Assembly", "label": "Sheet layout",
     "type": "enum", "values": ["grid", "strip"], "default": "grid",
     "applies_to": ["sheet"], "requires": "", "requires_value": "",
     "help": "Panel grid, or one equal-width vertical strip as the VACE wrapper builds it.",
     "value_help": {
         "grid": "Members are tiled into rows and columns, each panel the same size.",
         "strip": "Members are stacked as equal-width bands down one column, which is what the VACE wrapper expects.",
     }},
    {"key": "background", "section": "hard", "group": "Assembly", "label": "Sheet background",
     "type": "enum", "values": ["black", "white"], "default": "black",
     "applies_to": ["sheet"], "requires": "", "requires_value": "",
     "help": "Fill behind panels and padding. Ingredients documents black; the VACE wrapper pads white.",
     "value_help": {
         "black": "Black fill, which Ingredients documents and most sheet mechanisms assume.",
         "white": "White fill, which the kijai VACE wrapper pads with.",
     }},
    {"key": "output_size", "section": "hard", "group": "Geometry", "label": "Output size",
     "type": "enum", "values": ["scene", "native", "custom"], "default": "scene",
     "applies_to": list(IMAGE_ASSEMBLIES), "requires": "", "requires_value": "",
     "help": "Follow the scene resolution, keep each member's own aspect, or pin an exact size.",
     "value_help": {
         "scene": "Every member is fitted to the scene's render resolution.",
         "native": "Each member keeps its own aspect ratio, bounded by the long-edge maximum.",
         "custom": "Every member is fitted to an exact width and height this mechanism requires.",
     }},
    {"key": "width", "section": "hard", "group": "Geometry", "label": "Width",
     "type": "int", "min": 0, "max": 8192, "default": 0,
     "applies_to": list(IMAGE_ASSEMBLIES), "requires": "output_size", "requires_value": "custom",
     "help": "Exact output width this mechanism requires."},
    {"key": "height", "section": "hard", "group": "Geometry", "label": "Height",
     "type": "int", "min": 0, "max": 8192, "default": 0,
     "applies_to": list(IMAGE_ASSEMBLIES), "requires": "output_size", "requires_value": "custom",
     "help": "Exact output height this mechanism requires."},
    {"key": "long_edge_max", "section": "hard", "group": "Geometry", "label": "Long-edge maximum",
     "type": "int", "min": 16, "max": 8192, "default": 848,
     "applies_to": list(IMAGE_ASSEMBLIES), "requires": "output_size", "requires_value": "native",
     "help": "Upper bound on the longer side, so a large still is not passed through untouched."},
    {"key": "short_edge_max", "section": "hard", "group": "Geometry", "label": "Short-edge maximum",
     "type": "int", "min": 0, "max": 8192, "default": 0,
     "applies_to": list(IMAGE_ASSEMBLIES), "requires": "", "requires_value": "",
     "help": "Upper bound on the shorter side. 0 disables it; MiniMax H3 uses 2048."},
    {"key": "single_member_size", "section": "hard", "group": "Geometry", "label": "Lone-member size",
     "type": "int_pair", "min": 1, "max": 8192, "default": [],
     "applies_to": ["sheet"], "requires": "", "requires_value": "",
     "help": "Size used when only one member is staged, where a lone crop differs from the full sheet."},
    {"key": "size_multiple", "section": "hard", "group": "Geometry", "label": "Snap sides to",
     "type": "int", "min": 1, "max": 64, "default": 1,
     "applies_to": list(IMAGE_ASSEMBLIES), "requires": "", "requires_value": "",
     "help": "Both sides snap to this multiple. 1 leaves the size alone."},
    {"key": "size_multiple_source", "section": "hard", "group": "Geometry", "label": "Multiple taken from",
     "type": "enum", "values": ["custom", "template"], "default": "custom",
     "applies_to": list(IMAGE_ASSEMBLIES), "requires": "", "requires_value": "",
     "help": "Where the snap multiple comes from.",
     "value_help": {
         "custom": "The number you typed. It stays put whatever model the scene uses.",
         "template": "Pegged to the scene's model template, so it follows whenever you change model.",
     }},
    {"key": "frame_rate", "section": "hard", "group": "Frame rate", "label": "Reference FPS",
     "type": "number", "min": 0.001, "max": 240, "default": 24.0,
     "applies_to": ["batch", "slots"], "requires": "", "requires_value": "",
     "help": "Frame rate used when a video member is served as a sequence."},
    {"key": "frame_rate_source", "section": "hard", "group": "Frame rate", "label": "Reference FPS taken from",
     "type": "enum", "values": ["native", "scene", "custom"], "default": "scene",
     "applies_to": ["batch", "slots"], "requires": "", "requires_value": "",
     "help": "Native uses the first staged video member's rate; scene follows the effective scene rate; custom uses Reference FPS.",
     "value_help": {
         "native": "Use the first staged video member's native rate. Other video members resample to it.",
         "scene": "Follow the scene's effective frame rate, including project inheritance.",
         "custom": "Use the authored Reference FPS value.",
     }},
    {"key": "frame_step", "section": "hard", "group": "Frame grid", "label": "Frame step",
     "type": "int", "min": 1, "max": 64, "default": 8,
     "applies_to": ["sheet", "temporal"], "requires": "", "requires_value": "",
     "help": "Temporal VAE stride of the assembled reference sequence."},
    {"key": "frame_offset", "section": "hard", "group": "Frame grid", "label": "Frame offset",
     "type": "int", "min": 0, "max": 64, "default": 1,
     "applies_to": ["sheet", "temporal"], "requires": "", "requires_value": "",
     "help": "Grid offset, so valid lengths are step x k + offset."},
    {"key": "frame_grid_source", "section": "hard", "group": "Frame grid", "label": "Frame grid taken from",
     "type": "enum", "values": ["custom", "template"], "default": "custom",
     "applies_to": ["sheet", "temporal"], "requires": "", "requires_value": "",
     "help": "Where the frame step and offset come from.",
     "value_help": {
         "custom": "The step and offset above. They stay put whatever model the scene uses.",
         "template": "Pegged to the project's resolved frame constraint, so they follow whenever you change model. "
                     "Resolved at render time; the values above are the fallback when no constraint is set.",
     }},
    {"key": "allowed_frame_counts", "section": "hard", "group": "Frame grid", "label": "Allowed lengths",
     "type": "int_list", "min": 1, "max": 4096, "default": [],
     "applies_to": ["temporal"], "requires": "", "requires_value": "",
     "help": "Trained sequence lengths offered per staged item. 0 on the item auto-picks the shortest safe value."},
    {"key": "loop_frames", "section": "hard", "group": "Frame grid", "label": "Loop sheet to",
     "type": "int", "min": 0, "max": 4096, "default": 0,
     "applies_to": ["sheet"], "requires": "", "requires_value": "",
     "help": "Repeat the sheet to this many frames. Reference length only; it never limits the render window."},
    {"key": "loop_frames_source", "section": "hard", "group": "Frame grid", "label": "Loop length taken from",
     "type": "enum", "values": ["custom", "window"], "default": "custom",
     "applies_to": ["sheet"], "requires": "", "requires_value": "",
     "help": "Where the sheet loop length comes from.",
     "value_help": {
         "custom": "The fixed number above.",
         "window": "Pegged to the render window, so the sheet always spans the generation being produced.",
     }},
    {"key": "max_members", "section": "hard", "group": "Members", "label": "Maximum members",
     "type": "int", "min": 1, "max": 16, "default": 16,
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "Staging more than this refuses the render rather than dropping members silently."},
    {"key": "primary_model_position", "section": "hard", "group": "Members", "label": "Primary arrives",
     "type": "enum", "values": ["first", "last"], "default": "first",
     "applies_to": ["batch"], "requires": "", "requires_value": "",
     "help": "Some models silently move the primary reference to the end of the batch.",
     "value_help": {
         "first": "The first staged member arrives first, matching the order shown on the lane.",
         "last": "The primary reference is moved to the end of the batch, as SCAIL-2 does.",
     }},
    {"key": "live_outputs", "section": "hard", "group": "Bridge outputs", "label": "Outputs this recipe drives",
     "type": "output_list", "values": list(REFERENCE_OUTPUT_NAMES), "default": [],
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "Unchecked outputs read as unused on the Bridge and emit a type-correct fallback.",
     "value_help": {
          "image_slots": "The image bridge's r01-r16 block. Non-slot assemblies emit their single assembled batch or sequence on r01.",
          "audio_slots": "The audio bridge's a01-a16 block, one trimmed member per socket.",
          "reference_prompt": "Text derived from the staged members, or the item's override. It also drives p01-p16. Unchecked emits empty strings and collapses that block.",
          "reference_names": "Library names of the staged members in slot order. Unchecked emits an empty string.",
     }},
    {"key": "prompt_prefix", "section": "soft", "group": "Prompt", "label": "Prompt prefix",
     "type": "string", "default": "",
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "Static text placed once at the front of the whole derived prompt. It does not repeat "
             "per member - that is what the per-member pattern is for."},
    {"key": "prompt_tokens", "section": "soft", "group": "Prompt", "label": "Per-member token",
     "type": "string", "default": "",
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "Repeated once per staged member. Placeholders: {n} member number from 1, {index} from 0, "
             "{prompt} the member's own text, {name} its Library name. Use as many as you like, e.g. "
             "'<Subject {n}> is {prompt}, from <Picture {n}>'. With no {prompt}/{name} the member text is "
             "appended after the pattern. Each expansion is also emitted on its own p01-p16 output."},
    {"key": "suggested_tags", "section": "soft", "group": "Advisories", "label": "Suggested member tags",
     "type": "string_list", "default": [],
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "Staging without one of these raises a quality suggestion; it never blocks."},
    {"key": "context_tag", "section": "soft", "group": "Advisories", "label": "Background tag",
     "type": "string", "default": "",
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "Advisory tag for a background member. A context-class member is placed last in temporal assembly."},
    {"key": "recommended_duration_sec", "section": "soft", "group": "Advisories", "label": "Recommended seconds",
     "type": "number", "min": 0, "max": 3600, "default": 0,
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "Suggests a longer take when the staged audio is shorter than this."},
    {"key": "silent_single_input", "section": "soft", "group": "Advisories", "label": "Model reads one image",
     "type": "bool", "default": False,
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "Warns that the model consumes a single image, which is why members are composited."},
    {"key": "requires_identity_masks", "section": "soft", "group": "Advisories", "label": "Needs identity masks",
     "type": "bool", "default": False,
     "applies_to": [], "requires": "", "requires_value": "",
     "help": "Warns that this mechanism also needs colour-matched masks supplied outside the Bridge."},
)


def reference_recipe_field(key: str) -> dict | None:
    return next((field for field in REFERENCE_RECIPE_FIELDS if field["key"] == key), None)


def migrate_reference_recipe(recipe) -> dict:
    """Return one materialized/custom recipe with current hard-field vocabulary."""
    result = dict(recipe) if isinstance(recipe, dict) else {}
    if "hard" in result:
        result["hard"] = migrate_live_outputs(result.get("hard"))
    if isinstance(result.get("soft"), dict):
        result["soft"] = dict(result["soft"])
    return result


def migrate_reference_lane_recipe_data(value) -> dict:
    """Migrate the wrapper shape stored on scenes and frozen jobs."""
    result = dict(value) if isinstance(value, dict) else {}
    result["recipe"] = migrate_reference_recipe(result.get("recipe"))
    return result


def default_reference_class(kind: str) -> str:
    return "context" if str(kind or "") == "location" else "subject"


def normalize_reference_tags(raw_tags) -> list[str]:
    """Tolerantly normalize stored tags without interpreting unknown presets."""
    if not isinstance(raw_tags, list) or any(not isinstance(raw_tag, str) for raw_tag in raw_tags):
        return []
    result = []
    seen = set()
    for raw_tag in raw_tags:
        tag = " ".join(raw_tag.split())
        key = tag.casefold()
        if not tag or key in seen:
            continue
        result.append(tag)
        seen.add(key)
    return result


def normalize_reference_crop(raw_crop) -> dict | None:
    if raw_crop is None:
        return None
    if not isinstance(raw_crop, dict):
        return None
    try:
        crop = {key: float(raw_crop.get(key)) for key in ("x", "y", "w", "h")}
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) for value in crop.values()):
        return None
    if crop["x"] < 0 or crop["y"] < 0 or crop["w"] <= 0 or crop["h"] <= 0:
        return None
    if crop["x"] + crop["w"] > 1.0 + 1e-9 or crop["y"] + crop["h"] > 1.0 + 1e-9:
        return None
    return crop


def normalize_reference_source_range(raw_start, raw_end) -> tuple[float, float | None]:
    try:
        start = float(raw_start if raw_start is not None else 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0, None
    if not math.isfinite(start) or start < 0:
        return 0.0, None
    if raw_end is None or raw_end == "":
        return start, None
    try:
        end = float(raw_end)
    except (TypeError, ValueError, OverflowError):
        return 0.0, None
    if not math.isfinite(end) or end <= start:
        return 0.0, None
    return start, end


def classify_asset_path(path: str) -> tuple[str, str]:
    """Classify a path into an asset_type plus artifact_kind (if applicable)."""
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext in VIDEO_ASSET_EXTS:
        return "video", ""
    if ext in IMAGE_ASSET_EXTS:
        return "image", ""
    if ext in AUDIO_ASSET_EXTS:
        return "audio", ""
    return "artifact", ARTIFACT_KIND_BY_EXT.get(ext, "other")


@dataclass
class Asset:
    """A media file imported into the project, with metadata."""
    asset_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""                          # display name (e.g., "character_ref.png")
    asset_type: str = "video"               # video | image | audio | artifact
    artifact_kind: str = ""                 # latent | model | json | text | other
    path: str = ""                          # relative path inside project media/
    # Generation provenance — how was this asset created?
    prompt: str = ""
    generation_params: dict = field(default_factory=dict)  # seed, cfg, sampler, model, etc.
    # Technical metadata
    width: int = 0
    height: int = 0
    frame_count: int = 0                    # 0 for images/audio
    fps: float = 0.0                        # 0 for images/audio
    duration_sec: float = 0.0
    sample_rate: int = 0                    # audio only
    has_audio: bool = False                  # video files: True if video contains audio stream
    has_audio_checked: bool = False          # video files: audio probe has completed for current signature
    duration_checked: bool = False           # audio files: duration probe has completed for current signature
    media_probe_signature: str = ""          # file size + mtime marker for cached probes
    # Source color tags (video): normalized ffprobe/banner values, "" = untagged.
    # color_space "rgb" is a sentinel for matrix-free RGB content. A manually
    # set color_space overrides probing (mis-tagged-file escape hatch).
    color_space: str = ""
    color_transfer: str = ""
    color_primaries: str = ""
    color_range: str = ""
    color_probed: bool = False               # video files: color probe has completed for current signature
    imported_at: str = field(default_factory=lambda: datetime.now().isoformat())
    folder: str = ""                            # organizational folder (e.g., "Takes/Scene 1")
    favorite: bool = False                  # user-pinned in the asset gallery
    trashed_at: str = ""                    # ISO timestamp when moved to trash
    trash_previous_folder: str = ""         # folder before trashing, used for restore

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "name": self.name,
            "asset_type": self.asset_type,
            "artifact_kind": self.artifact_kind,
            "path": self.path,
            "prompt": self.prompt,
            "generation_params": self.generation_params,
            "width": self.width,
            "height": self.height,
            "frame_count": self.frame_count,
            "fps": self.fps,
            "duration_sec": self.duration_sec,
            "sample_rate": self.sample_rate,
            "has_audio": self.has_audio,
            "has_audio_checked": self.has_audio_checked,
            "duration_checked": self.duration_checked,
            "media_probe_signature": self.media_probe_signature,
            "color_space": self.color_space,
            "color_transfer": self.color_transfer,
            "color_primaries": self.color_primaries,
            "color_range": self.color_range,
            "color_probed": self.color_probed,
            "imported_at": self.imported_at,
            "folder": self.folder,
            "favorite": bool(self.favorite),
            "trashed_at": self.trashed_at,
            "trash_previous_folder": self.trash_previous_folder,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Asset":
        return cls(
            asset_id=data.get("asset_id", uuid.uuid4().hex[:8]),
            name=data.get("name", ""),
            asset_type=data.get("asset_type", "video"),
            artifact_kind=data.get("artifact_kind", ""),
            path=data.get("path", ""),
            prompt=data.get("prompt", ""),
            generation_params=data.get("generation_params", {}),
            width=data.get("width", 0),
            height=data.get("height", 0),
            frame_count=data.get("frame_count", 0),
            fps=data.get("fps", 0.0),
            duration_sec=data.get("duration_sec", 0.0),
            sample_rate=data.get("sample_rate", 0),
            has_audio=data.get("has_audio", False),
            has_audio_checked=data.get("has_audio_checked", False),
            duration_checked=data.get("duration_checked", False),
            media_probe_signature=data.get("media_probe_signature", ""),
            color_space=data.get("color_space", ""),
            color_transfer=data.get("color_transfer", ""),
            color_primaries=data.get("color_primaries", ""),
            color_range=data.get("color_range", ""),
            color_probed=data.get("color_probed", False),
            imported_at=data.get("imported_at", datetime.now().isoformat()),
            folder=data.get("folder", ""),
            favorite=bool(data.get("favorite", False)),
            trashed_at=data.get("trashed_at", ""),
            trash_previous_folder=data.get("trash_previous_folder", ""),
        )


# ---------------------------------------------------------------------------
# Reference Library — project-durable entities and asset membership
# ---------------------------------------------------------------------------

@dataclass
class ReferenceMember:
    member_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    asset_id: str = ""
    tags: list[str] = field(default_factory=list)
    prompt: str = ""
    crop: dict | None = None
    order: int = 0
    source_start_sec: float = 0.0
    source_end_sec: float | None = None

    def to_dict(self) -> dict:
        return {
            "member_id": self.member_id,
            "asset_id": self.asset_id,
            "tags": list(self.tags),
            "prompt": self.prompt,
            "crop": dict(self.crop) if isinstance(self.crop, dict) else None,
            "order": int(self.order),
            "source_start_sec": float(self.source_start_sec),
            "source_end_sec": None if self.source_end_sec is None else float(self.source_end_sec),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReferenceMember":
        if not isinstance(data, dict):
            data = {}
        start, end = normalize_reference_source_range(
            data.get("source_start_sec", 0.0),
            data.get("source_end_sec"),
        )
        try:
            order = int(data.get("order", 0))
        except (TypeError, ValueError, OverflowError):
            order = 0
        return cls(
            member_id=str(data.get("member_id", "") or ""),
            asset_id=str(data.get("asset_id", "") or ""),
            tags=normalize_reference_tags(data.get("tags")),
            prompt=str(data.get("prompt", "") or ""),
            crop=normalize_reference_crop(data.get("crop")),
            order=max(0, order),
            source_start_sec=start,
            source_end_sec=end,
        )


@dataclass
class ReferenceEntity:
    reference_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Untitled Reference"
    kind: str = "character"
    reference_class: str = "subject"
    # One-line identity for Library browsing, so a crowded Library stays
    # readable when the name alone is not enough. Project-only: nothing
    # composes it into a prompt — per-member prompt text is what reaches the
    # model. It replaced a longer free-form `notes` field, which is dropped on
    # load rather than migrated: the two overlapped, and neither had shipped.
    description: str = ""
    members: list[ReferenceMember] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "reference_id": self.reference_id,
            "name": self.name,
            "kind": self.kind,
            "reference_class": self.reference_class,
            "description": self.description,
            "members": [member.to_dict() for member in self.members],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReferenceEntity":
        if not isinstance(data, dict):
            data = {}
        kind = str(data.get("kind", "character") or "character")
        if kind not in REFERENCE_KINDS:
            kind = "character"
        reference_class = str(data.get("reference_class", "") or "")
        if reference_class not in REFERENCE_CLASSES:
            reference_class = default_reference_class(kind)
        name = str(data.get("name", "") or "").strip() or "Untitled Reference"

        raw_members = data.get("members", [])
        if not isinstance(raw_members, list):
            raw_members = []
        sortable_members = []
        for serialized_index, raw_member in enumerate(raw_members):
            member = ReferenceMember.from_dict(raw_member)
            # Ephemeral load metadata used only for deterministic identity
            # repair. Canonical serialization remains schema-clean.
            setattr(member, "_serialized_position", serialized_index)
            raw_order = raw_member.get("order") if isinstance(raw_member, dict) else None
            valid_order = isinstance(raw_order, int) and not isinstance(raw_order, bool) and raw_order >= 0
            order = raw_order if valid_order else 0
            sortable_members.append(((0, order, serialized_index) if valid_order else (1, 0, serialized_index), member))
        sortable_members.sort(key=lambda item: item[0])
        members = [member for _, member in sortable_members]
        for order, member in enumerate(members):
            member.order = order

        return cls(
            reference_id=str(data.get("reference_id", "") or ""),
            name=name,
            kind=kind,
            reference_class=reference_class,
            description=str(data.get("description", "") or ""),
            members=members,
        )


@dataclass
class ReferenceLaneRecipe:
    media_kind: str = "image"
    recipe_id: str = ""
    recipe: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "media_kind": "audio" if self.media_kind == "audio" else "image",
            "recipe_id": str(self.recipe_id or ""),
            "recipe": dict(self.recipe) if isinstance(self.recipe, dict) else {},
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReferenceLaneRecipe":
        if not isinstance(data, dict):
            data = {}
        media_kind = str(data.get("media_kind", "image") or "image")
        if media_kind not in {"image", "audio"}:
            media_kind = "image"
        recipe = migrate_reference_recipe(data.get("recipe", {}))
        return cls(
            media_kind=media_kind,
            recipe_id=str(data.get("recipe_id", "") or ""),
            recipe=dict(recipe) if isinstance(recipe, dict) else {},
        )


@dataclass
class ReferenceItem:
    reference_item_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    lane_index: int = 0
    start_frame: int = 0
    end_frame: int = -1
    members: list[dict] = field(default_factory=list)
    prompt_override: str = ""
    strength: float = 1.0
    sequence_frames: int = 0
    muted: bool = False

    def to_dict(self) -> dict:
        return {
            "reference_item_id": self.reference_item_id,
            "lane_index": int(self.lane_index),
            "start_frame": int(self.start_frame),
            "end_frame": int(self.end_frame),
            "members": [dict(member) for member in self.members if isinstance(member, dict)],
            "prompt_override": str(self.prompt_override or ""),
            "strength": float(self.strength),
            "sequence_frames": int(self.sequence_frames),
            "muted": bool(self.muted),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReferenceItem":
        if not isinstance(data, dict):
            data = {}
        members = []
        for raw_member in data.get("members", []) if isinstance(data.get("members", []), list) else []:
            if not isinstance(raw_member, dict):
                continue
            member_id = str(raw_member.get("member_id", "") or "")
            if not member_id:
                continue
            members.append({
                "entity_id": str(raw_member.get("entity_id", "") or ""),
                "member_id": member_id,
            })
        try:
            lane_index = max(0, int(data.get("lane_index", 0) or 0))
            start_frame = max(0, int(data.get("start_frame", 0) or 0))
            end_frame = int(data.get("end_frame", -1))
        except (TypeError, ValueError, OverflowError):
            lane_index, start_frame, end_frame = 0, 0, -1
        try:
            strength = float(data.get("strength", 1.0))
        except (TypeError, ValueError, OverflowError):
            strength = 1.0
        if not math.isfinite(strength):
            strength = 1.0
        strength = max(0.0, min(1.0, strength))
        try:
            sequence_frames = max(0, min(4096, int(data.get("sequence_frames", 0) or 0)))
        except (TypeError, ValueError, OverflowError):
            sequence_frames = 0
        if end_frame != -1 and end_frame <= start_frame:
            end_frame = start_frame + 1
        return cls(
            reference_item_id=str(data.get("reference_item_id", "") or ""),
            lane_index=lane_index,
            start_frame=start_frame,
            end_frame=end_frame,
            members=members,
            prompt_override=str(data.get("prompt_override", "") or ""),
            strength=strength,
            sequence_frames=sequence_frames,
            muted=bool(data.get("muted", False)),
        )


def _deterministic_reference_id(project_id: str, identity: str, seen: set[str]) -> str:
    attempt = 0
    while True:
        suffix = f"/{attempt}" if attempt else ""
        candidate = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"sonder-editor/{project_id}/{identity}{suffix}",
        ).hex
        if candidate not in seen:
            return candidate
        attempt += 1


def repair_reference_ids(project: "TimelineProject") -> None:
    """Repair blank/duplicate ids deterministically without performing a save."""
    seen_references: set[str] = set()
    seen_members: set[str] = set()
    project_id = str(getattr(project, "project_id", "") or "")
    for reference_index, reference in enumerate(getattr(project, "references", []) or []):
        raw_reference_id = str(getattr(reference, "reference_id", "") or "")
        reference_id = raw_reference_id if raw_reference_id.strip() else ""
        if not reference_id or reference_id in seen_references:
            reference_id = _deterministic_reference_id(
                project_id,
                f"reference/{reference_index}",
                seen_references,
            )
            reference.reference_id = reference_id
        seen_references.add(reference_id)
        serialized_members = sorted(
            enumerate(getattr(reference, "members", []) or []),
            key=lambda item: int(getattr(item[1], "_serialized_position", item[0])),
        )
        for member_index, member in serialized_members:
            serialized_position = int(getattr(member, "_serialized_position", member_index))
            raw_member_id = str(getattr(member, "member_id", "") or "")
            member_id = raw_member_id if raw_member_id.strip() else ""
            if not member_id or member_id in seen_members:
                member_id = _deterministic_reference_id(
                    project_id,
                    f"reference/{reference_index}/member/{serialized_position}",
                    seen_members,
                )
                member.member_id = member_id
            seen_members.add(member_id)


def effective_scene_fps(project, scene) -> float:
    """Return the scene's effective timeline rate with a safe legacy fallback."""
    try:
        scene_fps = float(getattr(scene, "fps", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        scene_fps = 0.0
    try:
        project_fps = float(getattr(project, "fps", 24.0) or 24.0)
    except (TypeError, ValueError, OverflowError):
        project_fps = 24.0
    fps = scene_fps if math.isfinite(scene_fps) and scene_fps > 0 else project_fps
    if not math.isfinite(fps) or fps <= 0:
        fps = 24.0
    return max(0.001, fps)


def _half_up(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def media_timeline_frames(asset: Asset, fps: float) -> int:
    """Return a media asset's duration in effective-scene frame units."""
    asset_type = str(getattr(asset, "asset_type", "") or "")
    if asset_type not in {"video", "audio"}:
        return 0
    try:
        timeline_fps = float(fps)
    except (TypeError, ValueError, OverflowError):
        timeline_fps = 0.0
    if not math.isfinite(timeline_fps) or timeline_fps <= 0:
        timeline_fps = 24.0

    try:
        duration_sec = float(getattr(asset, "duration_sec", 0.0) or 0.0)
    except (TypeError, ValueError, OverflowError):
        duration_sec = 0.0
    if math.isfinite(duration_sec) and duration_sec > 0:
        return max(0, _half_up(duration_sec * timeline_fps))

    if asset_type == "video":
        try:
            frame_count = int(float(getattr(asset, "frame_count", 0) or 0))
        except (TypeError, ValueError, OverflowError):
            frame_count = 0
        try:
            source_fps = float(getattr(asset, "fps", 0.0) or 0.0)
        except (TypeError, ValueError, OverflowError):
            source_fps = 0.0
        if frame_count > 0 and math.isfinite(source_fps) and source_fps > 0:
            return max(0, _half_up(frame_count * timeline_fps / source_fps))
        return max(0, frame_count)
    return 0


def _normalize_asset_relpath(path: str) -> str:
    value = str(path or "").replace("\\", "/").strip()
    while value.startswith("./"):
        value = value[2:]
    return value.casefold()


def apply_color_metadata(asset: "Asset", metadata: dict) -> bool:
    """Copy probed color fields from a media-metadata dict onto an asset.
    Returns True when anything changed."""
    changed = False
    for field in ("color_space", "color_transfer", "color_primaries", "color_range"):
        value = str(metadata.get(field, "") or "")
        if getattr(asset, field, "") != value:
            setattr(asset, field, value)
            changed = True
    probed = bool(metadata.get("color_probed", False))
    if getattr(asset, "color_probed", False) != probed:
        asset.color_probed = probed
        changed = True
    return changed


# ---------------------------------------------------------------------------
# Guide frames — reference images for generation consistency
# ---------------------------------------------------------------------------

@dataclass
class GuideFrame:
    """A reference image pinned to a specific frame index within a scene."""
    guide_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    frame_index: int = 0                    # 0 = first, -1 = last, or any absolute index
    asset_id: str = ""                      # points to an Asset in the project registry
    source: str = ""                        # "asset" | "scene_boundary" (auto from adjacent scene)
    strength: float = 1.0                   # conditioning strength 0.0-1.0
    muted: bool = False                     # hidden from editor/conditioning without deleting
    fit_mode: str = "pad_edge"              # fit | pad_edge | cover | stretch (default IS the fixed code constant)
    crop_position: str = "center"          # center | top | bottom | left | right (only meaningful for cover)

    def to_dict(self) -> dict:
        return {
            "guide_id": self.guide_id,
            "frame_index": self.frame_index,
            "asset_id": self.asset_id,
            "source": self.source,
            "strength": self.strength,
            "muted": self.muted,
            "fit_mode": self.fit_mode,
            "crop_position": self.crop_position,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GuideFrame":
        return cls(
            guide_id=data.get("guide_id", uuid.uuid4().hex[:8]),
            frame_index=data.get("frame_index", 0),
            asset_id=data.get("asset_id", ""),
            source=data.get("source", ""),
            strength=data.get("strength", 1.0),
            muted=bool(data.get("muted", False)),
            fit_mode=data.get("fit_mode", "pad_edge"),
            crop_position=data.get("crop_position", "center"),
        )


# ---------------------------------------------------------------------------
# Batch config — controls how a scene is split for GPU generation
# ---------------------------------------------------------------------------

@dataclass
class BatchConfig:
    """How to split a scene into GPU-sized generation batches."""
    max_frames: int = 97                    # max frames per batch (default: LTX 8k+1)
    context_overlap: int = 16               # overlap frames between batches for consistency
    frame_alignment: int = 8                # frames must satisfy (N - offset) % alignment == 0
    frame_offset: int = 1                   # LTX/Wan/Hunyuan are Nk+1; MiniMax H3 is 17k+5
    # e.g. LTX requires 8k+1: 9, 17, 25, ..., 97, ..., 193, 257

    def to_dict(self) -> dict:
        return {
            "max_frames": self.max_frames,
            "context_overlap": self.context_overlap,
            "frame_alignment": self.frame_alignment,
            "frame_offset": self.frame_offset,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchConfig":
        return cls(
            max_frames=data.get("max_frames", 97),
            context_overlap=data.get("context_overlap", 16),
            frame_alignment=data.get("frame_alignment", 8),
            frame_offset=data.get("frame_offset", 1),
        )

    def aligned_frame_count(self, desired: int) -> int:
        """Round desired frame count to the nearest valid value (alignment*k + offset)."""
        offset = max(1, int(self.frame_offset))
        if desired <= offset:
            return offset
        k = round((desired - offset) / self.frame_alignment)
        return max(k, 1) * self.frame_alignment + offset

    def compute_batches(self, total_frames: int) -> list[dict]:
        """Split total_frames into batches respecting alignment and overlap.

        Returns list of dicts with:
            batch_index, start_frame, end_frame, frame_count,
            context_start (frames re-used from previous batch)
        """
        if total_frames <= 0:
            return []

        aligned_total = self.aligned_frame_count(total_frames)

        # If it fits in a single batch, just return it
        if aligned_total <= self.max_frames:
            return [{
                "batch_index": 0,
                "start_frame": 0,
                "end_frame": aligned_total,
                "frame_count": aligned_total,
                "context_start": 0,
            }]

        usable = self.max_frames - self.context_overlap
        if usable <= 0:
            usable = self.max_frames

        batches = []
        pos = 0
        idx = 0
        while pos < aligned_total:
            remaining = aligned_total - pos
            batch_frames = min(self.max_frames, remaining)
            batch_frames = self.aligned_frame_count(batch_frames)

            context_start = self.context_overlap if idx > 0 else 0

            batches.append({
                "batch_index": idx,
                "start_frame": pos,
                "end_frame": pos + batch_frames,
                "frame_count": batch_frames,
                "context_start": context_start,
            })

            advance = batch_frames - (self.context_overlap if pos + batch_frames < aligned_total else 0)
            if advance <= 0:
                break
            pos += advance
            idx += 1

        return batches

    def remap_guide_index(self, absolute_index: int, total_frames: int,
                          batch: dict) -> int | None:
        """Remap an absolute guide frame index to a batch-local index.

        Returns None if the guide falls outside this batch.
        -1 is resolved to total_frames - 1 before remapping.
        """
        if absolute_index == -1:
            absolute_index = total_frames - 1

        if absolute_index < batch["start_frame"] or absolute_index >= batch["end_frame"]:
            return None

        return absolute_index - batch["start_frame"]


# ---------------------------------------------------------------------------
# Lane config — per-lane metadata (name, color, lock, hide/mute)
# ---------------------------------------------------------------------------

@dataclass
class LaneConfig:
    """Per-lane display/behavior settings."""
    name: str = ""          # Custom name (empty = use default "V0", "A1" etc.)
    color: str = ""         # Hex color (empty = use palette default)
    locked: bool = False    # Prevent edits
    hidden: bool = False    # Hidden from viewport (video) / muted (audio)

    def to_dict(self) -> dict:
        return {"name": self.name, "color": self.color, "locked": self.locked, "hidden": self.hidden}

    @classmethod
    def from_dict(cls, data: dict) -> "LaneConfig":
        if not isinstance(data, dict):
            data = {}
        return cls(
            name=data.get("name", ""),
            color=data.get("color", ""),
            locked=data.get("locked", False),
            hidden=data.get("hidden", False),
        )


# ---------------------------------------------------------------------------
# Scene — a composition segment (e.g., "dog eating", "bridge shot")
# ---------------------------------------------------------------------------

def _migrated_shot_timestamp(data: dict) -> bool:
    """`shot_timestamp` for a stored section, migrated to the ungated rule.

    The flag used to be gated behind `starts_new_shot` and defaulted True, so a
    section that opened no shot could carry a meaningless `True` that emitted
    nothing. Ungated, that stored value would suddenly stamp the section. Drop
    it: under the old rule the combination was unobservable, so this migration
    cannot change any existing project's output.
    """
    if not bool(data.get("shot_timestamp", False)):
        return False
    return bool(data.get("starts_new_shot", False))


class PromptSection:
    """A prompt assigned to a range of frames within a scene.

    `channels` ({visual, speech, sounds}) is the source of truth. The legacy
    flat `prompt` stays as a compatibility surface: constructing with
    `prompt=...` seeds the visual channel, assigning `.prompt` replaces the
    whole section text (visual = value, speech/sounds cleared), and reading
    `.prompt` composes label-free. Bracket labels are applied only at the
    composition points (slot 9 / snapshot freeze / bridge payload), never
    stored here.
    """

    def __init__(self, start_frame: int = 0, end_frame: int = 0,
                 prompt: str = "", channels: dict | None = None,
                 muted: bool = False, starts_new_shot: bool = False,
                 subject_ids: list | None = None, shot_timestamp: bool = False,
                 global_channel_exceptions: list | None = None):
        self.prompt_id = uuid.uuid4().hex[:8]
        self.start_frame = start_frame
        self.end_frame = end_frame
        self.muted = bool(muted)
        # Opens a new shot in the composed output. NOT inherited across a
        # split: a split is a range operation, and inheriting would open two
        # shots from one and shift every later [Shot N].
        self.starts_new_shot = bool(starts_new_shot)
        # Whether this section stamps its cut time. FULLY independent of
        # `starts_new_shot`: all four combinations are legal, including a bare
        # `At 00:07.000,` on a section that continues the current shot. Defaults
        # OFF — ungated, an on-by-default would stamp every section.
        self.shot_timestamp = bool(shot_timestamp)
        # Reference entities appearing in this section, as
        # [{entity_id, retention}, ...] — retention is a per-BINDING attribute,
        # which is why this is not a flat list of ids.
        self.subject_ids = prompt_payload.normalize_subject_ids(subject_ids)
        # Scene-global channels this section does NOT inherit. An EXCEPTIONS
        # set, not an inherit map: the default is to inherit everything, so a
        # channel added to the template later is inherited without touching a
        # single section, and an untouched project stores nothing.
        self.global_channel_exceptions = prompt_payload.normalize_channel_exceptions(
            global_channel_exceptions)
        if isinstance(channels, dict):
            self.channels = prompt_payload.normalize_channels(channels)
        else:
            self.channels = prompt_payload.normalize_channels(None, legacy_prompt=prompt)

    @property
    def prompt(self) -> str:
        return prompt_payload.compose_section_text(self.channels, labels_on=False)

    @prompt.setter
    def prompt(self, value):
        self.channels = prompt_payload.normalize_channels(None, legacy_prompt=value)

    def __eq__(self, other):
        if not isinstance(other, PromptSection):
            return NotImplemented
        return (self.start_frame == other.start_frame
                and self.end_frame == other.end_frame
                and self.channels == other.channels
                and self.starts_new_shot == other.starts_new_shot
                and self.shot_timestamp == other.shot_timestamp
                and self.subject_ids == other.subject_ids
                and self.global_channel_exceptions == other.global_channel_exceptions)

    def __repr__(self):
        return (f"PromptSection(start_frame={self.start_frame}, "
                f"end_frame={self.end_frame}, channels={self.channels!r})")

    def to_dict(self) -> dict:
        return {
            "prompt_id": self.prompt_id,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "channels": dict(self.channels),
            "muted": self.muted,
            "starts_new_shot": self.starts_new_shot,
            "shot_timestamp": self.shot_timestamp,
            "subject_ids": [dict(entry) for entry in self.subject_ids],
            "global_channel_exceptions": list(self.global_channel_exceptions),
            # Label-free composed mirror for older readers / downgrades.
            "prompt": self.prompt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PromptSection":
        if not isinstance(data, dict):
            data = {}
        raw_channels = data.get("channels")
        section = cls(
            start_frame=data.get("start_frame", 0),
            end_frame=data.get("end_frame", 0),
            prompt=data.get("prompt", ""),
            channels=raw_channels if isinstance(raw_channels, dict) else None,
            muted=bool(data.get("muted", False)),
            # Pre-upgrade dicts carry none of these keys; everything defaults
            # off/empty. See the migration note below for stored `True`s.
            starts_new_shot=bool(data.get("starts_new_shot", False)),
            shot_timestamp=_migrated_shot_timestamp(data),
            subject_ids=data.get("subject_ids"),
            global_channel_exceptions=data.get("global_channel_exceptions"),
        )
        section.prompt_id = data.get("prompt_id", uuid.uuid4().hex[:8])
        return section


@dataclass
class Scene:
    """A scene/composition within the project."""
    scene_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = "Untitled Scene"
    order: int = 0                          # position in the main composition
    duration_frames: int = 0                # desired total length (0 = empty/placeholder)
    prompt: str = ""                        # DERIVED label-free mirror of global_channels; see set_global_prompt
    global_channels: dict = field(default_factory=dict)  # scene-global prompt, per channel (source of truth)
    prompt_sections: list = field(default_factory=list)  # list[PromptSection]
    generation_params: dict = field(default_factory=dict)  # seed, cfg, sampler, model, etc.
    batch_config: BatchConfig = field(default_factory=BatchConfig)
    guide_frames: list = field(default_factory=list)    # list[GuideFrame]
    clips: list = field(default_factory=list)            # list[ClipReference] — generated segments
    audio_tracks: list = field(default_factory=list)     # list[AudioTrack]
    reference_items: list = field(default_factory=list)  # list[ReferenceItem]
    linked_item_groups: list = field(default_factory=list)  # list[{group_id, items:[{type,id}]}]
    asset_ids: list = field(default_factory=list)        # references to project-level Assets used
    is_bridge: bool = False                 # True if this is an auto-generated bridge between scenes
    video_lane_count: int = 1               # number of video lanes (multi-layer)
    motion_driver_lane_count: int = 1       # number of driver lanes
    audio_lane_count: int = 1               # number of audio lanes (multi-layer)
    reference_lane_count: int = 1           # number of Reference lanes
    video_lane_configs: list = field(default_factory=list)  # list[LaneConfig]
    motion_driver_lane_configs: list = field(default_factory=list)  # list[LaneConfig]
    audio_lane_configs: list = field(default_factory=list)  # list[LaneConfig]
    reference_lane_configs: list = field(default_factory=list)  # list[LaneConfig]
    reference_lane_recipes: list = field(default_factory=list)  # list[ReferenceLaneRecipe]
    guide_track_config: LaneConfig = field(default_factory=LaneConfig)
    prompt_track_config: LaneConfig = field(default_factory=LaneConfig)
    global_prompt_track_config: LaneConfig = field(default_factory=LaneConfig)
    width: int = 0                              # 0 = inherit from project
    height: int = 0                             # 0 = inherit from project
    fps: float = 0.0                            # 0 = inherit from project
    saved_selections: list = field(default_factory=list)  # list[dict] {name, start, end, pre/post context, mask pre/post offsets}

    def __post_init__(self):
        # A legacy scene carries only the flat `prompt`; seed the channels from
        # it so the two never start out disagreeing.
        if not isinstance(self.global_channels, dict) or not self.global_channels:
            self.global_channels = prompt_payload.normalize_channels(
                None, legacy_prompt=self.prompt or "")
        self._refresh_global_mirror()

    def _refresh_global_mirror(self) -> None:
        """Recompute the label-free `prompt` mirror from `global_channels`."""
        self.prompt = prompt_payload.compose_section_text(
            self.global_channels, labels_on=False)

    def set_global_prompt(self, value) -> None:
        """DESTRUCTIVE, mirroring `PromptSection.prompt`'s setter.

        Assigning the flat global prompt replaces the whole thing: the text
        lands in the first channel and every other channel is cleared. Every
        route that writes `body["prompt"]` must come through here, or an
        ordinary scene update would silently wipe global channels 2..n.
        """
        self.global_channels = prompt_payload.normalize_channels(
            None, legacy_prompt=str(value or ""))
        self._refresh_global_mirror()

    def set_global_channels(self, patch) -> None:
        """Merge a channel patch into the scene-global prompt.

        Merges rather than replaces for the same reason section channels do:
        a client only sends the channels of the template it is authoring under.
        """
        self.global_channels = prompt_payload.merge_channels(self.global_channels, patch)
        self._refresh_global_mirror()


    @property
    def duration_seconds(self) -> float:
        """Duration based on desired frames. Needs project fps to be accurate."""
        return 0.0  # caller must divide by fps

    @property
    def has_content(self) -> bool:
        """True if this scene has any generated clips."""
        return len(self.clips) > 0

    @property
    def total_clip_frames(self) -> int:
        """Actual frames of generated content (may differ from desired duration_frames)."""
        if not self.clips:
            return 0
        return max(c.timeline_end_frame for c in self.clips)

    def get_prompt_at_frame(self, frame: int, labels_on: bool = True,
                            delimiter: str = prompt_payload.DEFAULT_SECTION_DELIMITER,
                            boundary_threshold_pct: float = 0.0,
                            template=None, fps: float = 0.0) -> str:
        """Composed prompt (global + covering section) for a single frame."""
        return self.get_prompt_for_range(frame, frame + 1, labels_on=labels_on,
                                         delimiter=delimiter,
                                         boundary_threshold_pct=boundary_threshold_pct,
                                         template=template, fps=fps)

    def get_prompt_for_range(self, start: int, end: int, labels_on: bool = True,
                             delimiter: str = prompt_payload.DEFAULT_SECTION_DELIMITER,
                             boundary_threshold_pct: float = 0.0,
                             template=None, fps: float = 0.0) -> str:
        """Composed single-string prompt for a frame range.

        Global lane text + ALL segments overlapping the window in temporal
        order, joined by the section-seam delimiter (sections hold until the
        next section starts; the first also covers anything before it).
        Per-lane hidden semantics: global hidden zeroes the global part,
        segment lane hidden zeroes the section part, both hidden yields "".
        `boundary_threshold_pct` forwards the boundary-spill drop to the
        resolver (0 = off).
        """
        global_hidden = getattr(self.global_prompt_track_config, "hidden", False)
        sections_hidden = getattr(self.prompt_track_config, "hidden", False)
        global_text = "" if global_hidden else (self.prompt or "")
        # Raw, NOT pre-filtered: which global channels apply is a per-section
        # decision now, and only the composer knows which sections the window
        # actually reaches.
        global_channels = None if global_hidden else dict(self.global_channels or {})
        sections = [] if sections_hidden else self.prompt_sections
        return prompt_payload.compose_range_prompt(
            global_text, sections, start, end,
            labels_on=labels_on, delimiter=delimiter,
            boundary_threshold_pct=boundary_threshold_pct,
            template=template, fps=fps, global_channels=global_channels,
        )

    def to_dict(self) -> dict:
        def _safe_int(value, default=0):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        saved_selections = [
            {
                "name": entry.get("name", f"Selection {idx + 1}"),
                "start": _safe_int(entry.get("start", 0)),
                "end": _safe_int(entry.get("end", 0)),
                "pre_context_frames": _safe_int(entry.get("pre_context_frames", 0)),
                "post_context_frames": _safe_int(entry.get("post_context_frames", 0)),
                "mask_pre_offset": _safe_int(entry.get("mask_pre_offset", 0)),
                "mask_post_offset": _safe_int(entry.get("mask_post_offset", 0)),
            }
            for idx, entry in enumerate(self.saved_selections)
            if isinstance(entry, dict)
        ]

        return {
            "scene_id": self.scene_id,
            "name": self.name,
            "order": self.order,
            "duration_frames": self.duration_frames,
            # Derived at the persistence boundary, never trusted from the field,
            # so a stray direct assignment cannot be saved out of step with
            # the channels that actually compose.
            "prompt": prompt_payload.compose_section_text(
                self.global_channels, labels_on=False),
            "global_channels": dict(self.global_channels),
            "prompt_sections": [p.to_dict() for p in self.prompt_sections],
            "generation_params": self.generation_params,
            "batch_config": self.batch_config.to_dict(),
            "guide_frames": [g.to_dict() for g in self.guide_frames],
            "clips": [c.to_dict() for c in self.clips],
            "audio_tracks": [a.to_dict() for a in self.audio_tracks],
            "reference_items": [item.to_dict() for item in self.reference_items],
            "linked_item_groups": list(self.linked_item_groups),
            "asset_ids": list(self.asset_ids),
            "is_bridge": self.is_bridge,
            "video_lane_count": self.video_lane_count,
            "motion_driver_lane_count": self.motion_driver_lane_count,
            "audio_lane_count": self.audio_lane_count,
            "reference_lane_count": self.reference_lane_count,
            "video_lane_configs": [c.to_dict() for c in self.video_lane_configs],
            "motion_driver_lane_configs": [c.to_dict() for c in self.motion_driver_lane_configs],
            "audio_lane_configs": [c.to_dict() for c in self.audio_lane_configs],
            "reference_lane_configs": [c.to_dict() for c in self.reference_lane_configs],
            "reference_lane_recipes": [recipe.to_dict() for recipe in self.reference_lane_recipes],
            "guide_track_config": self.guide_track_config.to_dict(),
            "prompt_track_config": self.prompt_track_config.to_dict(),
            "global_prompt_track_config": self.global_prompt_track_config.to_dict(),
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "saved_selections": saved_selections,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Scene":
        def _safe_int(value, default=0):
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        scene = cls(
            scene_id=data.get("scene_id", uuid.uuid4().hex[:8]),
            name=data.get("name", "Untitled Scene"),
            order=data.get("order", 0),
            duration_frames=data.get("duration_frames", 0),
            prompt=data.get("prompt", ""),
            # Absent on a pre-upgrade scene; __post_init__ then seeds the
            # channels from the flat prompt above.
            global_channels=(data.get("global_channels")
                             if isinstance(data.get("global_channels"), dict) else {}),
            generation_params=data.get("generation_params", {}),
            batch_config=BatchConfig.from_dict(data.get("batch_config", {})),
            asset_ids=data.get("asset_ids", []),
            is_bridge=data.get("is_bridge", False),
            video_lane_count=data.get("video_lane_count", 1),
            motion_driver_lane_count=data.get("motion_driver_lane_count", 1),
            audio_lane_count=data.get("audio_lane_count", 1),
            reference_lane_count=data.get("reference_lane_count", 1),
            width=data.get("width", 0),
            height=data.get("height", 0),
            fps=data.get("fps", 0.0),
            saved_selections=[],
        )
        scene.saved_selections = [
            {
                "name": entry.get("name", f"Selection {idx + 1}"),
                "start": _safe_int(entry.get("start", 0)),
                "end": _safe_int(entry.get("end", 0)),
                "pre_context_frames": _safe_int(entry.get("pre_context_frames", 0)),
                "post_context_frames": _safe_int(entry.get("post_context_frames", 0)),
                "mask_pre_offset": _safe_int(entry.get("mask_pre_offset", 0)),
                "mask_post_offset": _safe_int(entry.get("mask_post_offset", 0)),
            }
            for idx, entry in enumerate(data.get("saved_selections", []))
            if isinstance(entry, dict)
        ]
        scene.prompt_sections = [
            PromptSection.from_dict(p) for p in data.get("prompt_sections", [])
        ]
        scene.guide_frames = [
            GuideFrame.from_dict(g) for g in data.get("guide_frames", [])
        ]
        scene.clips = [
            ClipReference.from_dict(c) for c in data.get("clips", [])
        ]
        scene.audio_tracks = [
            AudioTrack.from_dict(a) for a in data.get("audio_tracks", [])
        ]
        scene.reference_items = [
            ReferenceItem.from_dict(item) for item in data.get("reference_items", [])
        ]
        scene._ensure_stable_link_item_ids()
        scene.linked_item_groups = scene._normalize_linked_item_groups(
            data.get("linked_item_groups", [])
        )
        # Lane configs — deserialize + auto-pad to match lane counts
        for descriptor in VARIABLE_LANE_DESCRIPTORS:
            setattr(
                scene,
                descriptor.configs_attr,
                [
                    LaneConfig.from_dict(config)
                    for config in data.get(descriptor.configs_attr, [])
                ],
            )
        scene.reference_lane_recipes = [
            ReferenceLaneRecipe.from_dict(recipe)
            for recipe in data.get("reference_lane_recipes", [])
        ]
        pad_lane_configs(scene, LaneConfig)
        pad_lane_recipes(scene, ReferenceLaneRecipe)
        scene.guide_track_config = LaneConfig.from_dict(data.get("guide_track_config", {}))
        scene.prompt_track_config = LaneConfig.from_dict(data.get("prompt_track_config", {}))
        raw_global_config = data.get("global_prompt_track_config")
        if isinstance(raw_global_config, dict):
            scene.global_prompt_track_config = LaneConfig.from_dict(raw_global_config)
        else:
            # Migration seed: the legacy single prompt track's hidden flag muted
            # ALL prompt output including the fallback text. A pre-upgrade
            # hidden prompt track must not start re-emitting the old fallback
            # on slot 9 after the lane split.
            scene.global_prompt_track_config = LaneConfig(
                hidden=scene.prompt_track_config.hidden
            )
        return scene

    def _ensure_stable_link_item_ids(self) -> None:
        seen_guides = set()
        for guide in self.guide_frames:
            guide_id = str(getattr(guide, "guide_id", "") or "")
            if not guide_id or guide_id in seen_guides:
                guide_id = uuid.uuid4().hex[:8]
                guide.guide_id = guide_id
            seen_guides.add(guide_id)

        seen_prompts = set()
        for section in self.prompt_sections:
            prompt_id = str(getattr(section, "prompt_id", "") or "")
            if not prompt_id or prompt_id in seen_prompts:
                prompt_id = uuid.uuid4().hex[:8]
                section.prompt_id = prompt_id
            seen_prompts.add(prompt_id)

        seen_references = set()
        for item in self.reference_items:
            reference_item_id = str(getattr(item, "reference_item_id", "") or "")
            if not reference_item_id or reference_item_id in seen_references:
                reference_item_id = uuid.uuid4().hex
                item.reference_item_id = reference_item_id
            seen_references.add(reference_item_id)

    def _normalize_linked_item_groups(self, groups) -> list:
        if not isinstance(groups, list):
            return []
        existing = {
            "clip": {clip.clip_id for clip in self.clips},
            "audio": {track.track_id for track in self.audio_tracks},
            "guide": {guide.guide_id for guide in self.guide_frames},
            "prompt": {section.prompt_id for section in self.prompt_sections},
        }
        normalized = []
        seen_group_ids = set()
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("group_id", "") or "")
            if not group_id or group_id in seen_group_ids:
                group_id = uuid.uuid4().hex[:8]
            items = []
            seen_items = set()
            for item in group.get("items", []) or []:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type", "") or "")
                item_id = str(item.get("id", "") or "")
                key = (item_type, item_id)
                if item_id and item_id in existing.get(item_type, set()) and key not in seen_items:
                    items.append({"type": item_type, "id": item_id})
                    seen_items.add(key)
            if len(items) >= 2:
                normalized.append({"group_id": group_id, "items": items})
                seen_group_ids.add(group_id)
        return normalized


# ---------------------------------------------------------------------------
# Clip reference — a video segment on the timeline (within a scene)
# ---------------------------------------------------------------------------

@dataclass
class ClipReference:
    clip_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    source_path: str = ""
    timeline_start_frame: int = 0
    timeline_end_frame: int = 0
    source_in_frame: int = 0
    source_out_frame: int = 0
    total_source_frames: int = 0   # this piece's full source range (reset on split)
    source_origin_frame: int = 0   # source_in at creation/split (for trim ghost calc)
    opacity: float = 1.0
    track_index: int = 0
    role: str = "render"                    # render | motion_driver
    strength: float = 1.0                   # driver conditioning strength
    muted: bool = False                     # hidden from viewport/render/motion output
    fit_mode: str = "pad_edge"              # fit | pad_edge | cover | stretch (default IS the fixed code constant)
    crop_position: str = "center"          # center | top | bottom | left | right (only meaningful for cover)
    prompt: str = ""
    is_generated: bool = False
    generation_params: dict = field(default_factory=dict)
    takes: list = field(default_factory=list)
    active_take: int = 0
    take_metadata: dict = field(default_factory=dict)  # {scene_id, selection_start/end, prompt, context_frames}

    @property
    def duration_frames(self) -> int:
        return self.timeline_end_frame - self.timeline_start_frame

    @property
    def source_duration_frames(self) -> int:
        return self.source_out_frame - self.source_in_frame

    def to_dict(self) -> dict:
        return {
            "clip_id": self.clip_id,
            "source_path": self.source_path,
            "timeline_start_frame": self.timeline_start_frame,
            "timeline_end_frame": self.timeline_end_frame,
            "source_in_frame": self.source_in_frame,
            "source_out_frame": self.source_out_frame,
            "total_source_frames": self.total_source_frames,
            "source_origin_frame": self.source_origin_frame,
            "opacity": self.opacity,
            "track_index": self.track_index,
            "role": self.role,
            "strength": self.strength,
            "muted": self.muted,
            "fit_mode": self.fit_mode,
            "crop_position": self.crop_position,
            "prompt": self.prompt,
            "is_generated": self.is_generated,
            "generation_params": self.generation_params,
            "takes": list(self.takes),
            "active_take": self.active_take,
            "take_metadata": dict(self.take_metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClipReference":
        role = data.get("role", "render")
        if role not in {"render", "motion_driver"}:
            logger.warning("Unknown clip role %r; defaulting to render", role)
            role = "render"
        return cls(
            clip_id=data.get("clip_id", uuid.uuid4().hex[:8]),
            source_path=data.get("source_path", ""),
            timeline_start_frame=data.get("timeline_start_frame", 0),
            timeline_end_frame=data.get("timeline_end_frame", 0),
            source_in_frame=data.get("source_in_frame", 0),
            source_out_frame=data.get("source_out_frame", 0),
            total_source_frames=data.get("total_source_frames", 0),
            source_origin_frame=data.get("source_origin_frame", 0),
            opacity=data.get("opacity", 1.0),
            track_index=data.get("track_index", 0),
            role=role,
            strength=data.get("strength", 1.0),
            muted=bool(data.get("muted", False)),
            fit_mode=data.get("fit_mode", "pad_edge"),
            crop_position=data.get("crop_position", "center"),
            prompt=data.get("prompt", ""),
            is_generated=data.get("is_generated", False),
            generation_params=data.get("generation_params", {}),
            takes=data.get("takes", []),
            active_take=data.get("active_take", 0),
            take_metadata=data.get("take_metadata", {}),
        )


# ---------------------------------------------------------------------------
# Audio track
# ---------------------------------------------------------------------------

@dataclass
class AudioTrack:
    track_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    source_path: str = ""
    timeline_start_frame: int = 0
    timeline_end_frame: int = 0
    source_in_frame: int = 0      # offset into source audio (for trimming)
    total_source_frames: int = 0  # this piece's full source range (reset on split)
    source_origin_frame: int = 0  # source_in at creation/split (for trim ghost calc)
    volume: float = 1.0
    muted: bool = False
    lane_index: int = 0                 # audio lane (0-based, for multi-layer)

    def to_dict(self) -> dict:
        return {
            "track_id": self.track_id,
            "source_path": self.source_path,
            "timeline_start_frame": self.timeline_start_frame,
            "timeline_end_frame": self.timeline_end_frame,
            "source_in_frame": self.source_in_frame,
            "total_source_frames": self.total_source_frames,
            "source_origin_frame": self.source_origin_frame,
            "volume": self.volume,
            "muted": self.muted,
            "lane_index": self.lane_index,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AudioTrack":
        return cls(
            track_id=data.get("track_id", uuid.uuid4().hex[:8]),
            source_path=data.get("source_path", ""),
            timeline_start_frame=data.get("timeline_start_frame", 0),
            timeline_end_frame=data.get("timeline_end_frame", 0),
            source_in_frame=data.get("source_in_frame", 0),
            total_source_frames=data.get("total_source_frames", 0),
            source_origin_frame=data.get("source_origin_frame", 0),
            volume=data.get("volume", 1.0),
            muted=data.get("muted", False),
            lane_index=data.get("lane_index", 0),
        )


def retime_scene_geometry(scene: Scene, old_fps: float, new_fps: float) -> None:
    """Preserve scene time while changing its effective frame rate.

    Persisted timeline/source geometry is expressed in scene-time frame units,
    so every absolute endpoint follows the same half-up scale.
    """
    old_rate = float(old_fps)
    new_rate = float(new_fps)
    if not math.isfinite(old_rate) or old_rate <= 0:
        raise ValueError("old_fps must be finite and positive")
    if not math.isfinite(new_rate) or new_rate <= 0:
        raise ValueError("new_fps must be finite and positive")
    if old_rate == new_rate:
        return
    scale = new_rate / old_rate

    def scaled(value) -> int:
        return _half_up(int(value or 0) * scale)

    old_duration = max(0, int(getattr(scene, "duration_frames", 0) or 0))
    if old_duration > 0:
        scene.duration_frames = max(1, scaled(old_duration))

    for item in [*(getattr(scene, "clips", []) or []), *(getattr(scene, "audio_tracks", []) or [])]:
        old_origin = int(getattr(item, "source_origin_frame", 0) or 0)
        old_total = max(0, int(getattr(item, "total_source_frames", 0) or 0))
        for field_name in (
            "timeline_start_frame",
            "timeline_end_frame",
            "source_in_frame",
            "source_out_frame",
            "source_origin_frame",
        ):
            if hasattr(item, field_name):
                setattr(item, field_name, scaled(getattr(item, field_name, 0)))
        item.total_source_frames = max(0, scaled(old_origin + old_total) - scaled(old_origin))
        item.timeline_end_frame = max(
            int(item.timeline_start_frame) + 1,
            int(item.timeline_end_frame),
        )

    max_reference_start = max(0, int(getattr(scene, "duration_frames", 0) or 0) - 1)
    for item in getattr(scene, "reference_items", []) or []:
        start = scaled(getattr(item, "start_frame", 0))
        if int(getattr(scene, "duration_frames", 0) or 0) > 0:
            start = min(max_reference_start, max(0, start))
        old_end = int(getattr(item, "end_frame", -1))
        item.start_frame = start
        item.end_frame = -1 if old_end < 0 else max(start + 1, scaled(old_end))

    # Preserve the last-frame sentinel. For ordinary guides, process original
    # time order so the earlier guide keeps a rounded collision frame.
    guides = list(getattr(scene, "guide_frames", []) or [])
    ordinary_guides = [
        (index, guide, int(getattr(guide, "frame_index", 0) or 0))
        for index, guide in enumerate(guides)
        if int(getattr(guide, "frame_index", 0) or 0) >= 0
    ]
    ordinary_guides.sort(key=lambda entry: (entry[2], entry[0]))
    occupied = set()
    max_guide_frame = max(0, int(getattr(scene, "duration_frames", 0) or 0) - 1)
    for _index, guide, old_frame in ordinary_guides:
        frame = scaled(old_frame)
        if int(getattr(scene, "duration_frames", 0) or 0) > 0:
            frame = min(max_guide_frame, max(0, frame))
        while frame in occupied and frame < max_guide_frame:
            frame += 1
        if frame in occupied:
            # Saturated edge-case fallback: preserve uniqueness when an earlier
            # collision chain reaches the scene tail.
            frame = next((candidate for candidate in range(max_guide_frame + 1) if candidate not in occupied), frame)
        guide.frame_index = frame
        occupied.add(frame)

    sections = list(getattr(scene, "prompt_sections", []) or [])
    sections.sort(key=lambda section: (int(getattr(section, "start_frame", 0) or 0), int(getattr(section, "end_frame", 0) or 0)))
    previous_end = 0
    for section in sections:
        start = max(scaled(getattr(section, "start_frame", 0)), previous_end)
        end = max(start + 1, scaled(getattr(section, "end_frame", 0)))
        section.start_frame = start
        section.end_frame = end
        previous_end = end
    scene.prompt_sections = sections

    for selection in getattr(scene, "saved_selections", []) or []:
        if not isinstance(selection, dict):
            continue
        if "start" in selection:
            selection["start"] = scaled(selection.get("start", 0))
        if "end" in selection:
            selection["end"] = scaled(selection.get("end", 0))


# ---------------------------------------------------------------------------
# Generation job
# ---------------------------------------------------------------------------

@dataclass
class GenerationJob:
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    clip_id: str = ""
    scene_id: str = ""                      # which scene this job belongs to
    batch_index: int = 0                    # which batch within the scene
    batch_id: str = ""                      # shared ID for jobs created by one batch enqueue
    batch_total: int = 0                    # total jobs in the batch group
    status: str = "pending"                 # pending | running | completed | failed
    params: dict = field(default_factory=dict)
    progress: float = 0.0
    error: str = ""
    # Snapshot fields — capture state at queue time
    selection_start: int = 0
    selection_end: int = 0
    prompt: str = ""
    scene_prompt: str = ""                  # frozen global lane text ("" when global hidden at enqueue)
    scene_name: str = ""
    context_frames: int = 0
    pre_context_frames: int = 0
    post_context_frames: int = 0
    mask_pre_offset: int = 0
    mask_post_offset: int = 0
    guide_frame_snapshots: list = field(default_factory=list)
    driver_clip_snapshots: list = field(default_factory=list)
    driver_lane_count: int = 1
    driver_lane_configs: list = field(default_factory=list)
    reference_item_snapshots: list = field(default_factory=list)
    reference_lane_count: int = 1
    reference_lane_configs: list = field(default_factory=list)
    reference_lane_recipes: list = field(default_factory=list)
    prompt_sections: list = field(default_factory=list)
    scene_width: int = 0
    scene_height: int = 0
    scene_fps: float = 0.0
    template_id: str = "free"
    frame_constraint: dict | None = None
    dimension_constraint: dict | None = None
    take_placement_mode: str = "trimmed"
    take_placement_linked: bool = True
    take_placement_muted: bool = False
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    base_modified_at: str = ""
    completed_at: str = ""
    result_asset_id: str = ""

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "clip_id": self.clip_id,
            "scene_id": self.scene_id,
            "batch_index": self.batch_index,
            "batch_id": self.batch_id,
            "batch_total": self.batch_total,
            "status": self.status,
            "params": self.params,
            "progress": self.progress,
            "error": self.error,
            "selection_start": self.selection_start,
            "selection_end": self.selection_end,
            "prompt": self.prompt,
            "scene_prompt": self.scene_prompt,
            "scene_name": self.scene_name,
            "context_frames": self.context_frames,
            "pre_context_frames": self.pre_context_frames,
            "post_context_frames": self.post_context_frames,
            "mask_pre_offset": self.mask_pre_offset,
            "mask_post_offset": self.mask_post_offset,
            "guide_frame_snapshots": list(self.guide_frame_snapshots),
            "driver_clip_snapshots": list(self.driver_clip_snapshots),
            "driver_lane_count": self.driver_lane_count,
            "driver_lane_configs": list(self.driver_lane_configs),
            "reference_item_snapshots": list(self.reference_item_snapshots),
            "reference_lane_count": self.reference_lane_count,
            "reference_lane_configs": list(self.reference_lane_configs),
            "reference_lane_recipes": list(self.reference_lane_recipes),
            "prompt_sections": list(self.prompt_sections),
            "scene_width": self.scene_width,
            "scene_height": self.scene_height,
            "scene_fps": self.scene_fps,
            "template_id": self.template_id,
            "frame_constraint": self.frame_constraint,
            "dimension_constraint": self.dimension_constraint,
            "take_placement_mode": self.take_placement_mode,
            "take_placement_linked": self.take_placement_linked,
            "take_placement_muted": self.take_placement_muted,
            "created_at": self.created_at,
            "base_modified_at": self.base_modified_at,
            "completed_at": self.completed_at,
            "result_asset_id": self.result_asset_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GenerationJob":
        legacy_context = data.get("context_frames", 0)
        pre_context = data.get("pre_context_frames", legacy_context)
        post_context = data.get("post_context_frames", legacy_context)
        raw_mode = data.get("take_placement_mode", "trimmed")
        take_placement_mode = raw_mode if raw_mode in ("trimmed", "untrimmed") else "trimmed"
        return cls(
            job_id=data.get("job_id", uuid.uuid4().hex[:8]),
            clip_id=data.get("clip_id", ""),
            scene_id=data.get("scene_id", ""),
            batch_index=data.get("batch_index", 0),
            batch_id=data.get("batch_id", ""),
            batch_total=data.get("batch_total", 0),
            status=data.get("status", "pending"),
            params=data.get("params", {}),
            progress=data.get("progress", 0.0),
            error=data.get("error", ""),
            selection_start=data.get("selection_start", 0),
            selection_end=data.get("selection_end", 0),
            prompt=data.get("prompt", ""),
            scene_prompt=data.get("scene_prompt", ""),
            scene_name=data.get("scene_name", ""),
            context_frames=legacy_context,
            pre_context_frames=pre_context,
            post_context_frames=post_context,
            mask_pre_offset=data.get("mask_pre_offset", 0),
            mask_post_offset=data.get("mask_post_offset", 0),
            guide_frame_snapshots=list(data.get("guide_frame_snapshots", []) or []),
            driver_clip_snapshots=list(data.get("driver_clip_snapshots", []) or []),
            driver_lane_count=max(1, int(data.get("driver_lane_count", 1) or 1)),
            driver_lane_configs=list(data.get("driver_lane_configs", []) or []),
            reference_item_snapshots=list(data.get("reference_item_snapshots", []) or []),
            reference_lane_count=max(1, int(data.get("reference_lane_count", 1) or 1)),
            reference_lane_configs=list(data.get("reference_lane_configs", []) or []),
            reference_lane_recipes=[
                migrate_reference_lane_recipe_data(value)
                for value in (data.get("reference_lane_recipes", []) or [])
                if isinstance(value, dict)
            ],
            prompt_sections=list(data.get("prompt_sections", []) or []),
            scene_width=data.get("scene_width", 0),
            scene_height=data.get("scene_height", 0),
            scene_fps=data.get("scene_fps", 0.0),
            template_id=data.get("template_id", "free"),
            frame_constraint=data.get("frame_constraint"),
            dimension_constraint=data.get("dimension_constraint"),
            take_placement_mode=take_placement_mode,
            take_placement_linked=bool(data.get("take_placement_linked", data.get("take_linked", True))),
            take_placement_muted=bool(data.get("take_placement_muted", data.get("take_muted", False))),
            created_at=data.get("created_at", ""),
            base_modified_at=data.get("base_modified_at", ""),
            completed_at=data.get("completed_at", ""),
            result_asset_id=data.get("result_asset_id", ""),
        )


# ---------------------------------------------------------------------------
# Timeline project — top-level container
# ---------------------------------------------------------------------------

@dataclass
class TimelineProject:
    project_dir: str = ""
    project_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Untitled Project"
    fps: float = 24.0
    resolution: tuple = (1280, 720)
    template_id: str = "free"
    frame_constraint: dict | None = None
    dimension_constraint: dict | None = None
    scenes: list = field(default_factory=list)           # list[Scene] — ordered compositions
    assets: list = field(default_factory=list)           # list[Asset] — project media registry
    references: list = field(default_factory=list)       # list[ReferenceEntity] — project Reference Library
    reference_recipes: list = field(default_factory=list)  # project-durable custom Reference recipes
    generation_queue: list = field(default_factory=list)  # list[GenerationJob]
    metadata: dict = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    modified_at: str = field(default_factory=lambda: datetime.now().isoformat())

    # --- Scene helpers ---

    @property
    def total_frames(self) -> int:
        """Total frames across all scenes laid out sequentially."""
        return sum(s.duration_frames for s in self.scenes)

    @property
    def duration_seconds(self) -> float:
        return self.total_frames / self.fps if self.fps > 0 else 0.0

    def get_scene(self, scene_id: str) -> "Scene | None":
        for scene in self.scenes:
            if scene.scene_id == scene_id:
                return scene
        return None

    def add_scene(self, scene: "Scene") -> None:
        if scene.order == 0 and self.scenes:
            scene.order = max(s.order for s in self.scenes) + 1
        self.scenes.append(scene)
        self.modified_at = datetime.now().isoformat()

    def remove_scene(self, scene_id: str) -> bool:
        for i, scene in enumerate(self.scenes):
            if scene.scene_id == scene_id:
                self.scenes.pop(i)
                self.modified_at = datetime.now().isoformat()
                return True
        return False

    def scenes_ordered(self) -> list:
        """Return scenes sorted by their order field."""
        return sorted(self.scenes, key=lambda s: s.order)

    # --- Asset helpers ---

    def get_asset(self, asset_id: str) -> "Asset | None":
        for asset in self.assets:
            if asset.asset_id == asset_id:
                return asset
        return None

    def get_assets_by_type(self, asset_type: str) -> list:
        """Return assets filtered by type: 'video', 'image', 'audio', or 'artifact'."""
        return [a for a in self.assets if a.asset_type == asset_type]

    def asset_for_source_path(self, source_path: str) -> "Asset | None":
        """Registry lookup by normalized project-relative path (clip.source_path
        convention). Pure string comparison — no filesystem access."""
        normalized = _normalize_asset_relpath(source_path)
        if not normalized:
            return None
        for asset in self.assets:
            if _normalize_asset_relpath(getattr(asset, "path", "") or "") == normalized:
                return asset
        return None

    def add_asset(self, asset: "Asset") -> None:
        self.assets.append(asset)
        self.modified_at = datetime.now().isoformat()

    def remove_asset(self, asset_id: str) -> bool:
        for i, asset in enumerate(self.assets):
            if asset.asset_id == asset_id:
                self.assets.pop(i)
                self.modified_at = datetime.now().isoformat()
                return True
        return False

    # --- Legacy clip helpers (for backward compat during transition) ---

    @property
    def clips(self) -> list:
        """Aggregate all clips across all scenes."""
        result = []
        for scene in self.scenes:
            result.extend(scene.clips)
        return result

    @property
    def audio_tracks(self) -> list:
        """Aggregate all audio tracks across all scenes."""
        result = []
        for scene in self.scenes:
            result.extend(scene.audio_tracks)
        return result

    def get_clip(self, clip_id: str) -> "ClipReference | None":
        for scene in self.scenes:
            for clip in scene.clips:
                if clip.clip_id == clip_id:
                    return clip
        return None

    def add_clip(self, clip: "ClipReference") -> None:
        """Add clip to the first scene, or create a default scene."""
        if not self.scenes:
            self.add_scene(Scene(name="Scene 1", order=1))
        self.scenes[0].clips.append(clip)
        self.modified_at = datetime.now().isoformat()

    def remove_clip(self, clip_id: str) -> bool:
        for scene in self.scenes:
            for i, clip in enumerate(scene.clips):
                if clip.clip_id == clip_id:
                    scene.clips.pop(i)
                    self.modified_at = datetime.now().isoformat()
                    return True
        return False

    def add_audio_track(self, track: "AudioTrack") -> None:
        """Add audio to the first scene, or create a default scene."""
        if not self.scenes:
            self.add_scene(Scene(name="Scene 1", order=1))
        self.scenes[0].audio_tracks.append(track)
        self.modified_at = datetime.now().isoformat()

    # --- Serialization ---

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "fps": self.fps,
            "resolution": list(self.resolution),
            "template_id": self.template_id,
            "frame_constraint": self.frame_constraint,
            "dimension_constraint": self.dimension_constraint,
            "scenes": [s.to_dict() for s in self.scenes],
            "assets": [a.to_dict() for a in self.assets],
            "references": [reference.to_dict() for reference in self.references],
            "reference_recipes": [dict(recipe) for recipe in self.reference_recipes if isinstance(recipe, dict)],
            "generation_queue": [j.to_dict() for j in self.generation_queue],
            "metadata": self.metadata,
            "created_at": self.created_at,
            "modified_at": self.modified_at,
        }

    @classmethod
    def from_dict(cls, data: dict, project_dir: str = "") -> "TimelineProject":
        project = cls(
            project_dir=project_dir,
            project_id=data.get("project_id", uuid.uuid4().hex),
            name=data.get("name", "Untitled Project"),
            fps=data.get("fps", 24.0),
            resolution=tuple(data.get("resolution", [1280, 720])),
            template_id=data.get("template_id", "free"),
            frame_constraint=data.get("frame_constraint"),
            dimension_constraint=data.get("dimension_constraint"),
            metadata=data.get("metadata", {}),
            created_at=data.get("created_at", datetime.now().isoformat()),
            modified_at=data.get("modified_at", datetime.now().isoformat()),
        )
        project.scenes = [
            Scene.from_dict(s) for s in data.get("scenes", [])
        ]
        project.assets = [
            Asset.from_dict(a) for a in data.get("assets", [])
        ]
        raw_references = data.get("references", [])
        if not isinstance(raw_references, list):
            raw_references = []
        project.references = [
            ReferenceEntity.from_dict(reference) for reference in raw_references
        ]
        raw_reference_recipes = data.get("reference_recipes", [])
        if not isinstance(raw_reference_recipes, list):
            raw_reference_recipes = []
        project.reference_recipes = [
            migrate_reference_recipe(recipe) for recipe in raw_reference_recipes if isinstance(recipe, dict)
        ]
        repair_reference_ids(project)
        project.generation_queue = [
            GenerationJob.from_dict(j) for j in data.get("generation_queue", [])
        ]
        # Backward compat: migrate old flat clips/audio_tracks into a default scene
        old_clips = data.get("clips", [])
        old_audio = data.get("audio_tracks", [])
        if (old_clips or old_audio) and not data.get("scenes"):
            scene = Scene(name="Scene 1", order=1)
            scene.clips = [ClipReference.from_dict(c) for c in old_clips]
            scene.audio_tracks = [AudioTrack.from_dict(a) for a in old_audio]
            project.scenes.append(scene)
        return project
