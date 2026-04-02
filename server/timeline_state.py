import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# Asset registry — organized catalog of all project media
# ---------------------------------------------------------------------------

@dataclass
class Asset:
    """A media file imported into the project, with metadata."""
    asset_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""                          # display name (e.g., "character_ref.png")
    asset_type: str = "video"               # video | image | audio
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
    imported_at: str = field(default_factory=lambda: datetime.now().isoformat())
    folder: str = ""                            # organizational folder (e.g., "Takes/Scene 1")

    def to_dict(self) -> dict:
        return {
            "asset_id": self.asset_id,
            "name": self.name,
            "asset_type": self.asset_type,
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
            "imported_at": self.imported_at,
            "folder": self.folder,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Asset":
        return cls(
            asset_id=data.get("asset_id", uuid.uuid4().hex[:8]),
            name=data.get("name", ""),
            asset_type=data.get("asset_type", "video"),
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
            imported_at=data.get("imported_at", datetime.now().isoformat()),
            folder=data.get("folder", ""),
        )


# ---------------------------------------------------------------------------
# Guide frames — reference images for generation consistency
# ---------------------------------------------------------------------------

@dataclass
class GuideFrame:
    """A reference image pinned to a specific frame index within a scene."""
    frame_index: int = 0                    # 0 = first, -1 = last, or any absolute index
    asset_id: str = ""                      # points to an Asset in the project registry
    source: str = ""                        # "asset" | "scene_boundary" (auto from adjacent scene)
    strength: float = 1.0                   # conditioning strength 0.0-1.0

    def to_dict(self) -> dict:
        return {
            "frame_index": self.frame_index,
            "asset_id": self.asset_id,
            "source": self.source,
            "strength": self.strength,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GuideFrame":
        return cls(
            frame_index=data.get("frame_index", 0),
            asset_id=data.get("asset_id", ""),
            source=data.get("source", ""),
            strength=data.get("strength", 1.0),
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
        return cls(
            name=data.get("name", ""),
            color=data.get("color", ""),
            locked=data.get("locked", False),
            hidden=data.get("hidden", False),
        )


# ---------------------------------------------------------------------------
# Scene — a composition segment (e.g., "dog eating", "bridge shot")
# ---------------------------------------------------------------------------

@dataclass
class PromptSection:
    """A prompt assigned to a range of frames within a scene."""
    start_frame: int = 0
    end_frame: int = 0
    prompt: str = ""

    def to_dict(self) -> dict:
        return {
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "prompt": self.prompt,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PromptSection":
        return cls(
            start_frame=data.get("start_frame", 0),
            end_frame=data.get("end_frame", 0),
            prompt=data.get("prompt", ""),
        )


@dataclass
class Scene:
    """A scene/composition within the project."""
    scene_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = "Untitled Scene"
    order: int = 0                          # position in the main composition
    duration_frames: int = 0                # desired total length (0 = empty/placeholder)
    prompt: str = ""                        # fallback prompt when no sections defined
    prompt_sections: list = field(default_factory=list)  # list[PromptSection]
    generation_params: dict = field(default_factory=dict)  # seed, cfg, sampler, model, etc.
    batch_config: BatchConfig = field(default_factory=BatchConfig)
    guide_frames: list = field(default_factory=list)    # list[GuideFrame]
    clips: list = field(default_factory=list)            # list[ClipReference] — generated segments
    audio_tracks: list = field(default_factory=list)     # list[AudioTrack]
    asset_ids: list = field(default_factory=list)        # references to project-level Assets used
    is_bridge: bool = False                 # True if this is an auto-generated bridge between scenes
    video_lane_count: int = 1               # number of video lanes (multi-layer)
    audio_lane_count: int = 1               # number of audio lanes (multi-layer)
    video_lane_configs: list = field(default_factory=list)  # list[LaneConfig]
    audio_lane_configs: list = field(default_factory=list)  # list[LaneConfig]
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
                        c.source_in_frame, c.opacity, c.track_index)
                       for c in self.clips],
            "audio": [(a.source_path, a.timeline_start_frame, a.timeline_end_frame,
                        a.source_in_frame, a.volume, a.muted, a.lane_index)
                       for a in self.audio_tracks],
            "hidden_video": [i for i, c in enumerate(self.video_lane_configs) if c.hidden],
            "hidden_audio": [i for i, c in enumerate(self.audio_lane_configs) if c.hidden],
            "sel": (selection_start, selection_end),
            "res": resolution,
            "scene_res": (self.width, self.height),
            "scene_fps": self.fps,
        }
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]

    def get_prompt_at_frame(self, frame: int) -> str:
        """Return the prompt for a given frame. Falls back to scene-level prompt."""
        for section in self.prompt_sections:
            if section.start_frame <= frame < section.end_frame:
                return section.prompt
        return self.prompt

    def get_prompt_for_range(self, start: int, end: int) -> str:
        """Return the prompt covering a frame range. Uses first matching section."""
        for section in self.prompt_sections:
            if section.start_frame <= start and section.end_frame >= end:
                return section.prompt
            if section.start_frame < end and section.end_frame > start:
                return section.prompt
        return self.prompt

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
            "asset_ids": list(self.asset_ids),
            "is_bridge": self.is_bridge,
            "video_lane_count": self.video_lane_count,
            "audio_lane_count": self.audio_lane_count,
            "video_lane_configs": [c.to_dict() for c in self.video_lane_configs],
            "audio_lane_configs": [c.to_dict() for c in self.audio_lane_configs],
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
        # Lane configs — deserialize + auto-pad to match lane counts
        scene.video_lane_configs = [
            LaneConfig.from_dict(c) for c in data.get("video_lane_configs", [])
        ]
        while len(scene.video_lane_configs) < scene.video_lane_count:
            scene.video_lane_configs.append(LaneConfig())
        scene.audio_lane_configs = [
            LaneConfig.from_dict(c) for c in data.get("audio_lane_configs", [])
        ]
        while len(scene.audio_lane_configs) < scene.audio_lane_count:
            scene.audio_lane_configs.append(LaneConfig())
        return scene


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
            "prompt": self.prompt,
            "is_generated": self.is_generated,
            "generation_params": self.generation_params,
            "takes": list(self.takes),
            "active_take": self.active_take,
            "take_metadata": dict(self.take_metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ClipReference":
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
    status: str = "pending"                 # pending | running | completed | failed
    params: dict = field(default_factory=dict)
    progress: float = 0.0
    error: str = ""
    # Snapshot fields — capture state at queue time
    selection_start: int = 0
    selection_end: int = 0
    prompt: str = ""
    scene_name: str = ""
    context_frames: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    completed_at: str = ""
    result_asset_id: str = ""

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "clip_id": self.clip_id,
            "scene_id": self.scene_id,
            "batch_index": self.batch_index,
            "status": self.status,
            "params": self.params,
            "progress": self.progress,
            "error": self.error,
            "selection_start": self.selection_start,
            "selection_end": self.selection_end,
            "prompt": self.prompt,
            "scene_name": self.scene_name,
            "context_frames": self.context_frames,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "result_asset_id": self.result_asset_id,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "GenerationJob":
        return cls(
            job_id=data.get("job_id", uuid.uuid4().hex[:8]),
            clip_id=data.get("clip_id", ""),
            scene_id=data.get("scene_id", ""),
            batch_index=data.get("batch_index", 0),
            status=data.get("status", "pending"),
            params=data.get("params", {}),
            progress=data.get("progress", 0.0),
            error=data.get("error", ""),
            selection_start=data.get("selection_start", 0),
            selection_end=data.get("selection_end", 0),
            prompt=data.get("prompt", ""),
            scene_name=data.get("scene_name", ""),
            context_frames=data.get("context_frames", 0),
            created_at=data.get("created_at", ""),
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
        """Return assets filtered by type: 'video', 'image', or 'audio'."""
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
