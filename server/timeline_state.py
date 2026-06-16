import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
import os
from typing import Any

from . import prompt_payload

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
            imported_at=data.get("imported_at", datetime.now().isoformat()),
            folder=data.get("folder", ""),
            favorite=bool(data.get("favorite", False)),
            trashed_at=data.get("trashed_at", ""),
            trash_previous_folder=data.get("trash_previous_folder", ""),
        )


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
    frame_alignment: int = 8                # frames must satisfy (N-1) % alignment == 0
    # e.g. LTX requires 8k+1: 9, 17, 25, ..., 97, ..., 193, 257

    def to_dict(self) -> dict:
        return {
            "max_frames": self.max_frames,
            "context_overlap": self.context_overlap,
            "frame_alignment": self.frame_alignment,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BatchConfig":
        return cls(
            max_frames=data.get("max_frames", 97),
            context_overlap=data.get("context_overlap", 16),
            frame_alignment=data.get("frame_alignment", 8),
        )

    def aligned_frame_count(self, desired: int) -> int:
        """Round desired frame count to nearest valid aligned value (8k+1)."""
        if desired <= 1:
            return 1
        k = round((desired - 1) / self.frame_alignment)
        return max(k, 1) * self.frame_alignment + 1

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
                 muted: bool = False):
        self.prompt_id = uuid.uuid4().hex[:8]
        self.start_frame = start_frame
        self.end_frame = end_frame
        self.muted = bool(muted)
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
                and self.channels == other.channels)

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
    prompt: str = ""                        # scene-global prompt (always-on; also the fallback when no sections exist)
    prompt_sections: list = field(default_factory=list)  # list[PromptSection]
    generation_params: dict = field(default_factory=dict)  # seed, cfg, sampler, model, etc.
    batch_config: BatchConfig = field(default_factory=BatchConfig)
    guide_frames: list = field(default_factory=list)    # list[GuideFrame]
    clips: list = field(default_factory=list)            # list[ClipReference] — generated segments
    audio_tracks: list = field(default_factory=list)     # list[AudioTrack]
    linked_item_groups: list = field(default_factory=list)  # list[{group_id, items:[{type,id}]}]
    asset_ids: list = field(default_factory=list)        # references to project-level Assets used
    is_bridge: bool = False                 # True if this is an auto-generated bridge between scenes
    video_lane_count: int = 1               # number of video lanes (multi-layer)
    motion_driver_lane_count: int = 1       # single motion-driver lane in Phase 4.3
    audio_lane_count: int = 1               # number of audio lanes (multi-layer)
    video_lane_configs: list = field(default_factory=list)  # list[LaneConfig]
    motion_driver_lane_configs: list = field(default_factory=list)  # list[LaneConfig]
    audio_lane_configs: list = field(default_factory=list)  # list[LaneConfig]
    guide_track_config: LaneConfig = field(default_factory=LaneConfig)
    prompt_track_config: LaneConfig = field(default_factory=LaneConfig)
    global_prompt_track_config: LaneConfig = field(default_factory=LaneConfig)
    width: int = 0                              # 0 = inherit from project
    height: int = 0                             # 0 = inherit from project
    fps: float = 0.0                            # 0 = inherit from project
    saved_selections: list = field(default_factory=list)  # list[dict] {name, start, end, pre_context_frames, post_context_frames}

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

    def content_hash(self, selection_start: int = 0, selection_end: int = 0,
                     resolution: tuple = (768, 512)) -> str:
        """Deterministic hash of all state that affects rendered output."""
        import hashlib
        import json
        data = {
            "clips": [(c.source_path, c.timeline_start_frame, c.timeline_end_frame,
                        c.source_in_frame, c.opacity, c.track_index,
                        getattr(c, "role", "render"), getattr(c, "strength", 1.0),
                        getattr(c, "muted", False),
                        getattr(c, "fit_mode", "pad_edge"), getattr(c, "crop_position", "center"))
                       for c in self.clips],
            "guides": [(g.frame_index, g.asset_id, g.source,
                        getattr(g, "strength", 1.0), getattr(g, "muted", False),
                        getattr(g, "fit_mode", "pad_edge"), getattr(g, "crop_position", "center"))
                       for g in self.guide_frames],
            "audio": [(a.source_path, a.timeline_start_frame, a.timeline_end_frame,
                        a.source_in_frame, a.volume, a.muted, a.lane_index)
                       for a in self.audio_tracks],
            "hidden_video": [i for i, c in enumerate(self.video_lane_configs) if c.hidden],
            "hidden_motion_driver": [i for i, c in enumerate(self.motion_driver_lane_configs) if c.hidden],
            "hidden_audio": [i for i, c in enumerate(self.audio_lane_configs) if c.hidden],
            "hidden_guides": bool(getattr(self.guide_track_config, "hidden", False)),
            "sel": (selection_start, selection_end),
            "res": resolution,
            "scene_res": (self.width, self.height),
            "scene_fps": self.fps,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    def get_prompt_at_frame(self, frame: int, labels_on: bool = True,
                            delimiter: str = prompt_payload.DEFAULT_SECTION_DELIMITER) -> str:
        """Composed prompt (global + covering section) for a single frame."""
        return self.get_prompt_for_range(frame, frame + 1, labels_on=labels_on,
                                         delimiter=delimiter)

    def get_prompt_for_range(self, start: int, end: int, labels_on: bool = True,
                             delimiter: str = prompt_payload.DEFAULT_SECTION_DELIMITER) -> str:
        """Composed single-string prompt for a frame range.

        Global lane text + ALL segments overlapping the window in temporal
        order, joined by the section-seam delimiter (sections hold until the
        next section starts; the first also covers anything before it).
        Per-lane hidden semantics: global hidden zeroes the global part,
        segment lane hidden zeroes the section part, both hidden yields "".
        """
        global_hidden = getattr(self.global_prompt_track_config, "hidden", False)
        sections_hidden = getattr(self.prompt_track_config, "hidden", False)
        global_text = "" if global_hidden else (self.prompt or "")
        sections = [] if sections_hidden else self.prompt_sections
        return prompt_payload.compose_range_prompt(
            global_text, sections, start, end,
            labels_on=labels_on, delimiter=delimiter,
        )

    def to_dict(self) -> dict:
        return {
            "scene_id": self.scene_id,
            "name": self.name,
            "order": self.order,
            "duration_frames": self.duration_frames,
            "prompt": self.prompt,
            "prompt_sections": [p.to_dict() for p in self.prompt_sections],
            "generation_params": self.generation_params,
            "batch_config": self.batch_config.to_dict(),
            "guide_frames": [g.to_dict() for g in self.guide_frames],
            "clips": [c.to_dict() for c in self.clips],
            "audio_tracks": [a.to_dict() for a in self.audio_tracks],
            "linked_item_groups": list(self.linked_item_groups),
            "asset_ids": list(self.asset_ids),
            "is_bridge": self.is_bridge,
            "video_lane_count": self.video_lane_count,
            "motion_driver_lane_count": self.motion_driver_lane_count,
            "audio_lane_count": self.audio_lane_count,
            "video_lane_configs": [c.to_dict() for c in self.video_lane_configs],
            "motion_driver_lane_configs": [c.to_dict() for c in self.motion_driver_lane_configs],
            "audio_lane_configs": [c.to_dict() for c in self.audio_lane_configs],
            "guide_track_config": self.guide_track_config.to_dict(),
            "prompt_track_config": self.prompt_track_config.to_dict(),
            "global_prompt_track_config": self.global_prompt_track_config.to_dict(),
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "saved_selections": list(self.saved_selections),
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
            generation_params=data.get("generation_params", {}),
            batch_config=BatchConfig.from_dict(data.get("batch_config", {})),
            asset_ids=data.get("asset_ids", []),
            is_bridge=data.get("is_bridge", False),
            video_lane_count=data.get("video_lane_count", 1),
            motion_driver_lane_count=data.get("motion_driver_lane_count", 1),
            audio_lane_count=data.get("audio_lane_count", 1),
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
        scene._ensure_stable_link_item_ids()
        scene.linked_item_groups = scene._normalize_linked_item_groups(
            data.get("linked_item_groups", [])
        )
        # Lane configs — deserialize + auto-pad to match lane counts
        scene.video_lane_configs = [
            LaneConfig.from_dict(c) for c in data.get("video_lane_configs", [])
        ]
        while len(scene.video_lane_configs) < scene.video_lane_count:
            scene.video_lane_configs.append(LaneConfig())
        scene.motion_driver_lane_configs = [
            LaneConfig.from_dict(c) for c in data.get("motion_driver_lane_configs", [])
        ]
        while len(scene.motion_driver_lane_configs) < scene.motion_driver_lane_count:
            scene.motion_driver_lane_configs.append(LaneConfig())
        scene.audio_lane_configs = [
            LaneConfig.from_dict(c) for c in data.get("audio_lane_configs", [])
        ]
        while len(scene.audio_lane_configs) < scene.audio_lane_count:
            scene.audio_lane_configs.append(LaneConfig())
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
    strength: float = 1.0                   # motion-driver conditioning strength
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
    prompt_sections: list = field(default_factory=list)
    scene_width: int = 0
    scene_height: int = 0
    scene_fps: float = 0.0
    template_id: str = "free"
    frame_constraint: dict | None = None
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
            "prompt_sections": list(self.prompt_sections),
            "scene_width": self.scene_width,
            "scene_height": self.scene_height,
            "scene_fps": self.scene_fps,
            "template_id": self.template_id,
            "frame_constraint": self.frame_constraint,
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
            prompt_sections=list(data.get("prompt_sections", []) or []),
            scene_width=data.get("scene_width", 0),
            scene_height=data.get("scene_height", 0),
            scene_fps=data.get("scene_fps", 0.0),
            template_id=data.get("template_id", "free"),
            frame_constraint=data.get("frame_constraint"),
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
    resolution: tuple = (768, 512)
    template_id: str = "free"
    frame_constraint: dict | None = None
    scenes: list = field(default_factory=list)           # list[Scene] — ordered compositions
    assets: list = field(default_factory=list)           # list[Asset] — project media registry
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
            "scenes": [s.to_dict() for s in self.scenes],
            "assets": [a.to_dict() for a in self.assets],
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
            resolution=tuple(data.get("resolution", [768, 512])),
            template_id=data.get("template_id", "free"),
            frame_constraint=data.get("frame_constraint"),
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
