"""Tests for timeline state data types — serialization roundtrips."""

import sys
import os

# Add project root to path so we can import without ComfyUI
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.timeline_state import (
    Asset, GuideFrame, BatchConfig, Scene, PromptSection,
    ClipReference, AudioTrack, GenerationJob, TimelineProject, LaneConfig,
)


# --- Asset ---

def test_asset_roundtrip():
    asset = Asset(
        asset_id="img001",
        name="character_ref.png",
        asset_type="image",
        path="media/character_ref.png",
        prompt="a girl with red hair",
        generation_params={"seed": 42, "cfg": 7.5},
        width=768,
        height=512,
    )
    data = asset.to_dict()
    restored = Asset.from_dict(data)

    assert restored.asset_id == "img001"
    assert restored.name == "character_ref.png"
    assert restored.asset_type == "image"
    assert restored.prompt == "a girl with red hair"
    assert restored.generation_params == {"seed": 42, "cfg": 7.5}
    assert restored.width == 768


def test_asset_video_metadata():
    asset = Asset(
        asset_type="video",
        frame_count=120,
        fps=24.0,
        duration_sec=5.0,
        width=1920,
        height=1080,
    )
    data = asset.to_dict()
    restored = Asset.from_dict(data)

    assert restored.frame_count == 120
    assert restored.fps == 24.0
    assert restored.duration_sec == 5.0


def test_asset_audio_metadata():
    asset = Asset(
        asset_type="audio",
        duration_sec=30.5,
        sample_rate=44100,
    )
    data = asset.to_dict()
    restored = Asset.from_dict(data)

    assert restored.sample_rate == 44100
    assert restored.duration_sec == 30.5


def test_asset_probe_metadata_roundtrip():
    asset = Asset(
        asset_type="video",
        has_audio=True,
        has_audio_checked=True,
        duration_checked=True,
        media_probe_signature="123:456",
    )
    data = asset.to_dict()
    restored = Asset.from_dict(data)

    assert restored.has_audio_checked is True
    assert restored.duration_checked is True
    assert restored.media_probe_signature == "123:456"


def test_asset_trash_metadata_roundtrip():
    asset = Asset(
        asset_id="trash1",
        name="clip.mp4",
        asset_type="video",
        path="media/clip.mp4",
        folder="",
        trashed_at="2026-04-05T12:00:00",
        trash_previous_folder="Shots",
    )
    data = asset.to_dict()
    restored = Asset.from_dict(data)

    assert restored.trashed_at == "2026-04-05T12:00:00"
    assert restored.trash_previous_folder == "Shots"


def test_asset_favorite_metadata_roundtrip_and_legacy_default():
    asset = Asset(asset_id="fav1", asset_type="image", path="media/fav.png", favorite=True)
    data = asset.to_dict()
    restored = Asset.from_dict(data)
    legacy = Asset.from_dict({"asset_id": "legacy", "path": "media/legacy.png"})

    assert data["favorite"] is True
    assert restored.favorite is True
    assert legacy.favorite is False


# --- GuideFrame ---

def test_guide_frame_roundtrip():
    gf = GuideFrame(
        guide_id="guide-123",
        frame_index=123,
        asset_id="img001",
        source="asset",
        strength=0.8,
        muted=True,
        fit_mode="cover",
        crop_position="top",
    )
    data = gf.to_dict()
    restored = GuideFrame.from_dict(data)

    assert data["guide_id"] == "guide-123"
    assert restored.guide_id == "guide-123"
    assert restored.frame_index == 123
    assert restored.asset_id == "img001"
    assert restored.source == "asset"
    assert restored.strength == 0.8
    assert restored.muted is True
    assert restored.fit_mode == "cover"
    assert restored.crop_position == "top"


def test_fit_mode_defaults_to_constant_for_legacy_records():
    # Legacy project.json has no fit keys → deserialize to the fixed constants,
    # NOT a browser default, so renders are deterministic across browsers.
    clip = ClipReference.from_dict({"clip_id": "c1"})
    guide = GuideFrame.from_dict({"frame_index": 0})
    assert (clip.fit_mode, clip.crop_position) == ("pad_edge", "center")
    assert (guide.fit_mode, guide.crop_position) == ("pad_edge", "center")


def test_guide_frame_last_frame():
    gf = GuideFrame(frame_index=-1, asset_id="img002")
    assert gf.frame_index == -1


# --- BatchConfig ---

def test_batch_config_roundtrip():
    bc = BatchConfig(max_frames=193, context_overlap=24, frame_alignment=8)
    data = bc.to_dict()
    restored = BatchConfig.from_dict(data)

    assert restored.max_frames == 193
    assert restored.context_overlap == 24
    assert restored.frame_alignment == 8


def test_batch_config_aligned_frame_count():
    bc = BatchConfig(frame_alignment=8)
    # 8k+1 pattern: 1, 9, 17, 25, ..., 97, ..., 193
    assert bc.aligned_frame_count(1) == 1
    assert bc.aligned_frame_count(9) == 9
    assert bc.aligned_frame_count(10) == 9      # rounds to nearest
    assert bc.aligned_frame_count(13) == 17     # rounds up
    assert bc.aligned_frame_count(97) == 97
    assert bc.aligned_frame_count(100) == 97    # rounds to nearest


def test_batch_config_compute_batches_single():
    bc = BatchConfig(max_frames=97, context_overlap=16, frame_alignment=8)
    batches = bc.compute_batches(90)
    assert len(batches) == 1
    assert batches[0]["batch_index"] == 0
    assert batches[0]["context_start"] == 0
    # 90 frames -> aligned to 8k+1 = 89 (k=11), fits in one batch of max 97
    assert batches[0]["frame_count"] == 89


def test_batch_config_compute_batches_multiple():
    bc = BatchConfig(max_frames=97, context_overlap=16, frame_alignment=8)
    batches = bc.compute_batches(200)
    assert len(batches) >= 2
    # First batch has no context
    assert batches[0]["context_start"] == 0
    # Subsequent batches have context overlap
    for b in batches[1:]:
        assert b["context_start"] == 16


def test_batch_config_compute_batches_empty():
    bc = BatchConfig()
    assert bc.compute_batches(0) == []


def test_batch_config_remap_guide_index():
    bc = BatchConfig(max_frames=97, context_overlap=16, frame_alignment=8)
    batch = {"start_frame": 81, "end_frame": 178, "frame_count": 97, "context_start": 16}

    # Guide at absolute frame 100 -> local frame 19
    assert bc.remap_guide_index(100, 200, batch) == 19

    # Guide at frame 0 -> outside this batch
    assert bc.remap_guide_index(0, 200, batch) is None

    # Guide at -1 (last frame) with total 200 -> absolute 199 -> outside if batch ends at 178
    assert bc.remap_guide_index(-1, 200, batch) is None

    # Guide at -1 with total 170 -> absolute 169 -> local 88
    assert bc.remap_guide_index(-1, 170, batch) == 88


# --- Scene ---

def test_scene_roundtrip():
    scene = Scene(
        scene_id="sc001",
        name="Dog Eating",
        order=2,
        duration_frames=360,
        prompt="a dog eating food from a bowl",
        generation_params={"seed": 123, "cfg": 7.0},
        is_bridge=False,
    )
    scene.guide_frames = [
        GuideFrame(guide_id="guide-1", frame_index=0, asset_id="img001", source="asset"),
        GuideFrame(guide_id="guide-2", frame_index=-1, asset_id="img002", source="asset"),
    ]
    scene.linked_item_groups = [{
        "group_id": "links-1",
        "items": [{"type": "guide", "id": "guide-1"}, {"type": "guide", "id": "guide-2"}],
    }]
    scene.asset_ids = ["img001", "img002", "vid001"]
    scene.saved_selections = [{
        "name": "Main beat",
        "start": 48,
        "end": 96,
        "pre_context_frames": 8,
        "post_context_frames": 12,
    }]

    data = scene.to_dict()
    restored = Scene.from_dict(data)

    assert restored.scene_id == "sc001"
    assert restored.name == "Dog Eating"
    assert restored.order == 2
    assert restored.duration_frames == 360
    assert restored.prompt == "a dog eating food from a bowl"
    assert len(restored.guide_frames) == 2
    assert restored.guide_frames[0].frame_index == 0
    assert restored.guide_frames[1].frame_index == -1
    assert restored.linked_item_groups == scene.linked_item_groups
    assert restored.asset_ids == ["img001", "img002", "vid001"]
    assert restored.saved_selections[0]["pre_context_frames"] == 8
    assert restored.saved_selections[0]["post_context_frames"] == 12
    assert restored.is_bridge is False


def test_scene_saved_selection_defaults_context_fields():
    scene = Scene.from_dict({
        "name": "Legacy Scene",
        "saved_selections": [{"name": "Old", "start": "4", "end": "20"}],
    })

    assert scene.saved_selections == [{
        "name": "Old",
        "start": 4,
        "end": 20,
        "pre_context_frames": 0,
        "post_context_frames": 0,
    }]


def test_scene_empty():
    scene = Scene(name="Placeholder", duration_frames=120)
    assert not scene.has_content
    assert scene.total_clip_frames == 0


def test_scene_with_clips():
    scene = Scene(name="Test")
    scene.clips = [
        ClipReference(timeline_start_frame=0, timeline_end_frame=97),
        ClipReference(timeline_start_frame=81, timeline_end_frame=178),
    ]
    assert scene.has_content
    assert scene.total_clip_frames == 178


def test_scene_bridge():
    scene = Scene(name="Bridge 1-2", is_bridge=True, duration_frames=48)
    data = scene.to_dict()
    restored = Scene.from_dict(data)
    assert restored.is_bridge is True


def test_scene_motion_driver_lane_config_roundtrip_and_autopad():
    scene = Scene(
        name="Motion",
        motion_driver_lane_count=2,
        motion_driver_lane_configs=[LaneConfig(name="Driver", color="#2a9b9e", locked=True, hidden=True)],
    )

    data = scene.to_dict()
    restored = Scene.from_dict(data)

    assert restored.motion_driver_lane_count == 2
    assert len(restored.motion_driver_lane_configs) == 2
    assert restored.motion_driver_lane_configs[0].name == "Driver"
    assert restored.motion_driver_lane_configs[0].color == "#2a9b9e"
    assert restored.motion_driver_lane_configs[0].locked is True
    assert restored.motion_driver_lane_configs[0].hidden is True
    assert isinstance(restored.motion_driver_lane_configs[1], LaneConfig)


def test_scene_fixed_track_config_roundtrip_and_defaults():
    scene = Scene(
        name="Fixed",
        guide_track_config=LaneConfig(locked=True, hidden=True),
        prompt_track_config=LaneConfig(locked=True, hidden=False),
    )

    restored = Scene.from_dict(scene.to_dict())

    assert restored.guide_track_config.locked is True
    assert restored.guide_track_config.hidden is True
    assert restored.prompt_track_config.locked is True
    assert restored.prompt_track_config.hidden is False

    legacy = Scene.from_dict({"name": "Legacy"})
    assert legacy.guide_track_config.locked is False
    assert legacy.guide_track_config.hidden is False
    assert legacy.prompt_track_config.locked is False
    assert legacy.prompt_track_config.hidden is False


def test_scene_content_hash_changes_when_clip_role_changes_render_visibility():
    base_clip = ClipReference(
        source_path="media/clip.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        source_in_frame=0,
        opacity=1.0,
        track_index=0,
    )
    render_scene = Scene(name="Render")
    render_scene.clips = [base_clip]

    driver_scene = Scene(name="Render")
    driver_scene.clips = [ClipReference.from_dict({**base_clip.to_dict(), "role": "motion_driver"})]

    assert render_scene.content_hash() != driver_scene.content_hash()


def test_scene_content_hash_ignores_driver_only_state():
    driver_clip = ClipReference(
        source_path="media/driver.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
        role="motion_driver",
        strength=0.25,
        muted=False,
    )
    base_scene = Scene(name="Render", motion_driver_lane_count=1)
    base_scene.clips = [driver_clip]
    base_scene.motion_driver_lane_configs = [LaneConfig(hidden=False, name="Driver 1")]
    base_hash = base_scene.content_hash()

    changed_scene = Scene(name="Render", motion_driver_lane_count=1)
    changed_scene.clips = [
        ClipReference.from_dict({
            **driver_clip.to_dict(),
            "strength": 0.9,
            "muted": True,
        }),
    ]
    changed_scene.motion_driver_lane_configs = [LaneConfig(hidden=True, name="Renamed Driver")]

    assert changed_scene.content_hash() == base_hash


def test_scene_content_hash_changes_with_clip_and_guide_mute_state():
    clip = ClipReference(
        source_path="media/clip.mp4",
        timeline_start_frame=0,
        timeline_end_frame=10,
    )
    base_scene = Scene(name="Render")
    base_scene.clips = [clip]
    base_scene.guide_frames = [GuideFrame(frame_index=0, asset_id="guide-a")]

    muted_clip_scene = Scene(name="Render")
    muted_clip_scene.clips = [ClipReference.from_dict({**clip.to_dict(), "muted": True})]
    muted_clip_scene.guide_frames = [GuideFrame(frame_index=0, asset_id="guide-a")]

    muted_guide_scene = Scene(name="Render")
    muted_guide_scene.clips = [ClipReference.from_dict(clip.to_dict())]
    muted_guide_scene.guide_frames = [GuideFrame(frame_index=0, asset_id="guide-a", muted=True)]

    assert base_scene.content_hash() != muted_clip_scene.content_hash()
    assert base_scene.content_hash() != muted_guide_scene.content_hash()

    hidden_guides_scene = Scene(name="Render")
    hidden_guides_scene.clips = [ClipReference.from_dict(clip.to_dict())]
    hidden_guides_scene.guide_frames = [GuideFrame(frame_index=0, asset_id="guide-a")]
    hidden_guides_scene.guide_track_config = LaneConfig(hidden=True)

    assert base_scene.content_hash() != hidden_guides_scene.content_hash()


def test_scene_content_hash_changes_with_fit_mode_and_crop_position():
    clip = ClipReference(source_path="media/clip.mp4", timeline_start_frame=0, timeline_end_frame=10)
    base_scene = Scene(name="Render")
    base_scene.clips = [clip]
    base_scene.guide_frames = [GuideFrame(frame_index=0, asset_id="guide-a")]

    clip_fit_scene = Scene(name="Render")
    clip_fit_scene.clips = [ClipReference.from_dict({**clip.to_dict(), "fit_mode": "cover"})]
    clip_fit_scene.guide_frames = [GuideFrame(frame_index=0, asset_id="guide-a")]

    clip_crop_scene = Scene(name="Render")
    clip_crop_scene.clips = [ClipReference.from_dict({**clip.to_dict(), "fit_mode": "cover", "crop_position": "top"})]
    clip_crop_scene.guide_frames = [GuideFrame(frame_index=0, asset_id="guide-a")]

    guide_fit_scene = Scene(name="Render")
    guide_fit_scene.clips = [ClipReference.from_dict(clip.to_dict())]
    guide_fit_scene.guide_frames = [GuideFrame(frame_index=0, asset_id="guide-a", fit_mode="stretch")]

    assert base_scene.content_hash() != clip_fit_scene.content_hash()
    assert clip_fit_scene.content_hash() != clip_crop_scene.content_hash()
    assert base_scene.content_hash() != guide_fit_scene.content_hash()


def test_scene_content_hash_stable_for_legacy_fit_default():
    # A scene whose stored clip/guide dicts predate the fit fields must hash the
    # same as one explicitly carrying the default constants (no spurious cache bust
    # between a freshly-saved default item and a legacy one).
    clip_legacy = ClipReference.from_dict({"source_path": "media/clip.mp4",
                                           "timeline_start_frame": 0, "timeline_end_frame": 10})
    clip_explicit = ClipReference.from_dict({"source_path": "media/clip.mp4",
                                             "timeline_start_frame": 0, "timeline_end_frame": 10,
                                             "fit_mode": "pad_edge", "crop_position": "center"})
    legacy_scene = Scene(name="Render")
    legacy_scene.clips = [clip_legacy]
    explicit_scene = Scene(name="Render")
    explicit_scene.clips = [clip_explicit]
    assert legacy_scene.content_hash() == explicit_scene.content_hash()


# --- ClipReference ---

def test_clip_reference_roundtrip():
    clip = ClipReference(
        clip_id="abc123",
        source_path="/path/to/video.mp4",
        timeline_start_frame=0,
        timeline_end_frame=120,
        source_in_frame=10,
        source_out_frame=130,
        track_index=0,
        role="motion_driver",
        strength=0.42,
        muted=True,
        fit_mode="stretch",
        crop_position="right",
        prompt="a cat walking",
        is_generated=True,
        generation_params={"seed": 42, "cfg": 7.5},
        takes=["/path/take0.mp4", "/path/take1.mp4"],
        active_take=1,
    )

    data = clip.to_dict()
    restored = ClipReference.from_dict(data)

    assert restored.clip_id == clip.clip_id
    assert restored.source_path == clip.source_path
    assert restored.timeline_start_frame == clip.timeline_start_frame
    assert restored.timeline_end_frame == clip.timeline_end_frame
    assert restored.source_in_frame == clip.source_in_frame
    assert restored.source_out_frame == clip.source_out_frame
    assert restored.role == "motion_driver"
    assert restored.strength == 0.42
    assert restored.muted is True
    assert restored.fit_mode == "stretch"
    assert restored.crop_position == "right"
    assert restored.prompt == clip.prompt
    assert restored.is_generated == clip.is_generated
    assert restored.generation_params == clip.generation_params
    assert restored.takes == clip.takes
    assert restored.active_take == clip.active_take


def test_clip_reference_duration():
    clip = ClipReference(timeline_start_frame=10, timeline_end_frame=50)
    assert clip.duration_frames == 40


def test_clip_reference_role_defaults_and_unknown_clamps(caplog):
    legacy = ClipReference.from_dict({"clip_id": "legacy"})
    assert legacy.role == "render"
    assert legacy.strength == 1.0

    unknown = ClipReference.from_dict({"clip_id": "bad", "role": "mystery", "strength": 0.7})
    assert unknown.role == "render"
    assert unknown.strength == 0.7
    assert "Unknown clip role" in caplog.text


# --- AudioTrack (unchanged) ---

def test_audio_track_roundtrip():
    track = AudioTrack(
        track_id="aud001",
        source_path="/path/to/audio.wav",
        timeline_start_frame=0,
        timeline_end_frame=2400,
        volume=0.8,
        muted=True,
    )

    data = track.to_dict()
    restored = AudioTrack.from_dict(data)

    assert restored.track_id == track.track_id
    assert restored.source_path == track.source_path
    assert restored.volume == 0.8
    assert restored.muted is True


# --- GenerationJob ---

def test_generation_job_roundtrip():
    job = GenerationJob(
        job_id="job001",
        clip_id="clip001",
        scene_id="sc001",
        batch_index=2,
        batch_id="batch-123",
        batch_total=4,
        status="running",
        params={"steps": 20},
        progress=0.5,
        selection_start=24,
        selection_end=72,
        prompt="queued prompt",
        scene_name="Scene 1",
        context_frames=12,
        pre_context_frames=8,
        post_context_frames=12,
        mask_pre_offset=2,
        mask_post_offset=3,
        guide_frame_snapshots=[{"frame_index": 12, "asset_id": "guide001", "source": "asset", "strength": 0.8}],
        driver_clip_snapshots=[{
            "clip_id": "driver-1",
            "source_path": "media/driver.mp4",
            "timeline_start_frame": 8,
            "timeline_end_frame": 20,
            "track_index": 1,
            "role": "motion_driver",
            "strength": 0.65,
        }],
        driver_lane_count=2,
        driver_lane_configs=[{"name": "Canny", "hidden": True}, {"name": "Depth", "hidden": False}],
        prompt_sections=[{"start_frame": 0, "end_frame": 96, "prompt": "section prompt"}],
        scene_width=1024,
        scene_height=576,
        scene_fps=30.0,
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1, "max": 257},
        take_placement_mode="untrimmed",
        take_placement_linked=False,
        take_placement_muted=True,
    )

    data = job.to_dict()
    restored = GenerationJob.from_dict(data)

    assert restored.job_id == job.job_id
    assert restored.scene_id == "sc001"
    assert restored.batch_index == 2
    assert restored.batch_id == "batch-123"
    assert restored.batch_total == 4
    assert restored.status == "running"
    assert restored.progress == 0.5
    assert restored.selection_start == 24
    assert restored.selection_end == 72
    assert restored.prompt == "queued prompt"
    assert restored.scene_name == "Scene 1"
    assert restored.context_frames == 12
    assert restored.pre_context_frames == 8
    assert restored.post_context_frames == 12
    assert restored.mask_pre_offset == 2
    assert restored.mask_post_offset == 3
    assert restored.guide_frame_snapshots[0]["asset_id"] == "guide001"
    assert restored.driver_clip_snapshots[0]["clip_id"] == "driver-1"
    assert restored.driver_clip_snapshots[0]["strength"] == 0.65
    assert restored.driver_lane_count == 2
    assert restored.driver_lane_configs[0]["name"] == "Canny"
    assert restored.driver_lane_configs[0]["hidden"] is True
    assert restored.prompt_sections[0]["prompt"] == "section prompt"
    assert restored.scene_width == 1024
    assert restored.scene_height == 576
    assert restored.scene_fps == 30.0
    assert restored.template_id == "ltx-2.3"
    assert restored.frame_constraint == {"step": 8, "offset": 1, "min": 1, "max": 257}
    assert restored.take_placement_mode == "untrimmed"
    assert restored.take_placement_linked is False
    assert restored.take_placement_muted is True


def test_generation_job_legacy_context_frames_migrate_to_pre_and_post():
    restored = GenerationJob.from_dict({
        "job_id": "legacy",
        "context_frames": 16,
    })

    assert restored.context_frames == 16
    assert restored.pre_context_frames == 16
    assert restored.post_context_frames == 16
    assert restored.mask_pre_offset == 0
    assert restored.mask_post_offset == 0


def test_generation_job_invalid_take_placement_mode_defaults_to_trimmed():
    restored = GenerationJob.from_dict({
        "job_id": "bad-mode",
        "take_placement_mode": "wide-open",
    })

    assert restored.take_placement_mode == "trimmed"


# --- TimelineProject ---

def test_timeline_project_roundtrip():
    project = TimelineProject(
        project_dir="/tmp/test_project",
        project_id="proj123",
        name="Test Project",
        fps=30.0,
        resolution=(1920, 1080),
        template_id="ltx-2.3",
        frame_constraint={"step": 8, "offset": 1, "min": 1, "max": 257},
    )

    scene = Scene(scene_id="sc1", name="Scene 1", order=1, duration_frames=90)
    scene.clips = [
        ClipReference(clip_id="c1", source_path="/tmp/vid.mp4",
                      timeline_start_frame=0, timeline_end_frame=90),
    ]
    scene.audio_tracks = [
        AudioTrack(track_id="a1", source_path="/tmp/audio.wav",
                   timeline_end_frame=900),
    ]
    project.add_scene(scene)

    asset = Asset(asset_id="img001", name="ref.png", asset_type="image",
                  path="media/ref.png")
    project.add_asset(asset)

    data = project.to_dict()
    restored = TimelineProject.from_dict(data, project_dir="/tmp/test_project")

    assert restored.project_id == "proj123"
    assert restored.name == "Test Project"
    assert restored.fps == 30.0
    assert restored.resolution == (1920, 1080)
    assert restored.template_id == "ltx-2.3"
    assert restored.frame_constraint == {"step": 8, "offset": 1, "min": 1, "max": 257}
    assert len(restored.scenes) == 1
    assert restored.scenes[0].scene_id == "sc1"
    assert len(restored.scenes[0].clips) == 1
    assert restored.scenes[0].clips[0].clip_id == "c1"
    assert len(restored.scenes[0].audio_tracks) == 1
    assert len(restored.assets) == 1
    assert restored.assets[0].asset_id == "img001"


def test_project_total_frames():
    project = TimelineProject(fps=24.0)
    assert project.total_frames == 0
    assert project.duration_seconds == 0.0

    project.add_scene(Scene(name="S1", order=1, duration_frames=48))
    assert project.total_frames == 48
    assert project.duration_seconds == 2.0

    project.add_scene(Scene(name="S2", order=2, duration_frames=72))
    assert project.total_frames == 120
    assert project.duration_seconds == 5.0


def test_project_scene_management():
    project = TimelineProject()
    scene = Scene(scene_id="find_me", name="Test", duration_frames=24)
    project.add_scene(scene)

    assert project.get_scene("find_me") is scene
    assert project.get_scene("nonexistent") is None

    assert project.remove_scene("find_me") is True
    assert len(project.scenes) == 0
    assert project.remove_scene("find_me") is False


def test_project_asset_management():
    project = TimelineProject()
    video = Asset(asset_id="v1", asset_type="video", name="clip.mp4")
    image = Asset(asset_id="i1", asset_type="image", name="ref.png")
    audio = Asset(asset_id="a1", asset_type="audio", name="bg.wav")
    project.add_asset(video)
    project.add_asset(image)
    project.add_asset(audio)

    assert len(project.assets) == 3
    assert len(project.get_assets_by_type("video")) == 1
    assert len(project.get_assets_by_type("image")) == 1
    assert len(project.get_assets_by_type("audio")) == 1
    assert project.get_asset("v1") is video
    assert project.remove_asset("v1") is True
    assert len(project.assets) == 2


def test_project_scenes_ordered():
    project = TimelineProject()
    project.add_scene(Scene(name="Third", order=3))
    project.add_scene(Scene(name="First", order=1))
    project.add_scene(Scene(name="Second", order=2))

    ordered = project.scenes_ordered()
    assert [s.name for s in ordered] == ["First", "Second", "Third"]


def test_project_legacy_clip_helpers():
    """Backward compat: add_clip/add_audio_track still work via scenes."""
    project = TimelineProject()
    clip = ClipReference(clip_id="c1", timeline_end_frame=24)
    project.add_clip(clip)

    assert len(project.scenes) == 1
    assert project.scenes[0].name == "Scene 1"
    assert len(project.clips) == 1
    assert project.get_clip("c1") is clip
    assert project.remove_clip("c1") is True
    assert len(project.clips) == 0


def test_project_backward_compat_migration():
    """Old project.json with flat clips/audio_tracks gets migrated to a scene."""
    old_data = {
        "project_id": "old_proj",
        "name": "Legacy Project",
        "fps": 24.0,
        "resolution": [768, 512],
        "clips": [
            {"clip_id": "c1", "timeline_end_frame": 48},
        ],
        "audio_tracks": [
            {"track_id": "a1", "timeline_end_frame": 960},
        ],
    }
    project = TimelineProject.from_dict(old_data, project_dir="/tmp/legacy")

    assert len(project.scenes) == 1
    assert project.scenes[0].name == "Scene 1"
    assert len(project.scenes[0].clips) == 1
    assert project.scenes[0].clips[0].clip_id == "c1"
    assert len(project.scenes[0].audio_tracks) == 1


def test_project_resolution_tuple_preserved():
    """Ensure resolution survives JSON roundtrip as tuple."""
    project = TimelineProject(resolution=(1280, 720))
    data = project.to_dict()
    assert data["resolution"] == [1280, 720]  # JSON serializes as list

    restored = TimelineProject.from_dict(data)
    assert restored.resolution == (1280, 720)  # from_dict converts back to tuple


# --- PromptSection ---

def test_prompt_section_roundtrip():
    ps = PromptSection(start_frame=0, end_frame=100, prompt="girl feeds dog")
    ps.prompt_id = "prompt-123"
    ps.muted = True
    data = ps.to_dict()
    restored = PromptSection.from_dict(data)
    assert data["prompt_id"] == "prompt-123"
    assert data["muted"] is True
    assert restored.prompt_id == "prompt-123"
    assert restored.start_frame == 0
    assert restored.end_frame == 100
    assert restored.prompt == "girl feeds dog"
    assert restored.muted is True
    # Legacy flat prompt seeds the visual channel
    assert restored.channels == {"visual": "girl feeds dog", "speech": "", "sounds": ""}


def test_prompt_section_channels_roundtrip():
    ps = PromptSection(start_frame=5, end_frame=20,
                       channels={"visual": "close-up", "speech": "hello", "sounds": "rain"})
    data = ps.to_dict()
    assert data["channels"] == {"visual": "close-up", "speech": "hello", "sounds": "rain"}
    # Serialized prompt mirror is the label-free compose
    assert data["prompt"] == "close-up hello rain"
    restored = PromptSection.from_dict(data)
    assert restored.channels == ps.channels
    # channels wins over the prompt mirror on load
    assert restored.prompt == "close-up hello rain"


def test_prompt_section_legacy_write_contract():
    # Legacy kwarg construction still works (routes + old tests construct this way)
    ps = PromptSection(start_frame=0, end_frame=10, prompt="flat text")
    assert ps.channels["visual"] == "flat text"
    # Assigning .prompt replaces the whole section text: visual set, others cleared
    ps.channels = {"visual": "v", "speech": "s", "sounds": "n"}
    ps.prompt = "replacement"
    assert ps.channels == {"visual": "replacement", "speech": "", "sounds": ""}
    # Reading .prompt composes label-free
    ps.channels = {"visual": "a", "speech": "b", "sounds": ""}
    assert ps.prompt == "a b"


def test_scene_with_prompt_sections():
    scene = Scene(
        name="Test",
        duration_frames=200,
        prompt="fallback prompt",
        prompt_sections=[
            PromptSection(start_frame=0, end_frame=100, prompt="first half"),
            PromptSection(start_frame=100, end_frame=200, prompt="second half"),
        ],
    )

    # Roundtrip
    data = scene.to_dict()
    assert len(data["prompt_sections"]) == 2

    restored = Scene.from_dict(data)
    assert len(restored.prompt_sections) == 2
    assert restored.prompt_sections[0].prompt == "first half"
    assert restored.prompt_sections[1].prompt == "second half"


def test_scene_get_prompt_at_frame():
    scene = Scene(
        prompt="global style",
        prompt_sections=[
            PromptSection(start_frame=0, end_frame=100, prompt="section A"),
            PromptSection(start_frame=100, end_frame=200, prompt="section B"),
        ],
    )

    # Composed output: global lane text + covering section (labels on by default)
    assert scene.get_prompt_at_frame(0) == "global style [VISUAL]: section A"
    assert scene.get_prompt_at_frame(99) == "global style [VISUAL]: section A"
    assert scene.get_prompt_at_frame(100) == "global style [VISUAL]: section B"
    # Hold-until-next: the last section's tail extends past its drawn end
    assert scene.get_prompt_at_frame(200) == "global style [VISUAL]: section B"
    assert scene.get_prompt_at_frame(999) == "global style [VISUAL]: section B"
    # Labels off
    assert scene.get_prompt_at_frame(0, labels_on=False) == "global style section A"


def test_scene_get_prompt_for_range():
    scene = Scene(
        prompt="global style",
        prompt_sections=[
            PromptSection(start_frame=0, end_frame=100, prompt="section A"),
            PromptSection(start_frame=100, end_frame=200, prompt="section B"),
        ],
    )

    assert scene.get_prompt_for_range(0, 100) == "global style [VISUAL]: section A"
    assert scene.get_prompt_for_range(100, 200) == "global style [VISUAL]: section B"
    # Range spanning both — ALL window segments concatenated in temporal
    # order, joined by the section-seam delimiter (default "."). Labels are
    # channel-grouped: one [VISUAL]: prefix, never repeated per segment.
    assert scene.get_prompt_for_range(50, 150) == (
        "global style [VISUAL]: section A. section B"
    )
    assert scene.get_prompt_for_range(50, 150, delimiter=",") == (
        "global style [VISUAL]: section A, section B"
    )
    # Range past the last drawn end — the last section holds (single segment)
    assert scene.get_prompt_for_range(200, 300) == "global style [VISUAL]: section B"


def test_scene_prompt_hidden_matrix():
    def build():
        return Scene(
            prompt="global style",
            prompt_sections=[
                PromptSection(start_frame=0, end_frame=100, prompt="section A"),
            ],
        )

    # Segment lane hidden → global only
    scene = build()
    scene.prompt_track_config = LaneConfig(hidden=True)
    assert scene.get_prompt_at_frame(50) == "global style"
    assert scene.get_prompt_for_range(0, 100) == "global style"

    # Global lane hidden → section only
    scene = build()
    scene.global_prompt_track_config = LaneConfig(hidden=True)
    assert scene.get_prompt_for_range(0, 100) == "[VISUAL]: section A"

    # Both hidden → empty (matches the legacy single-track rule)
    scene = build()
    scene.prompt_track_config = LaneConfig(hidden=True)
    scene.global_prompt_track_config = LaneConfig(hidden=True)
    assert scene.get_prompt_at_frame(50) == ""
    assert scene.get_prompt_for_range(0, 100) == ""


def test_global_prompt_config_migration_seed():
    # Pre-upgrade scene dict: hidden prompt track muted ALL prompt output.
    # The absent global config must seed hidden from the legacy flag so
    # slot 9 does not start re-emitting the old fallback text after upgrade.
    legacy = Scene.from_dict({
        "prompt": "old fallback",
        "prompt_track_config": {"hidden": True},
    })
    assert legacy.global_prompt_track_config.hidden is True
    assert legacy.get_prompt_for_range(0, 100) == ""

    # Visible legacy track seeds a visible global lane
    visible = Scene.from_dict({"prompt": "old fallback"})
    assert visible.global_prompt_track_config.hidden is False
    assert visible.get_prompt_for_range(0, 100) == "old fallback"

    # An explicit stored global config wins over the seed
    explicit = Scene.from_dict({
        "prompt_track_config": {"hidden": True},
        "global_prompt_track_config": {"hidden": False, "locked": True},
    })
    assert explicit.global_prompt_track_config.hidden is False
    assert explicit.global_prompt_track_config.locked is True


def test_scene_no_prompt_sections_uses_fallback():
    scene = Scene(prompt="the only prompt", prompt_sections=[])
    assert scene.get_prompt_at_frame(0) == "the only prompt"
    assert scene.get_prompt_for_range(0, 100) == "the only prompt"


def test_content_hash_excludes_prompt_state():
    # Prompts never affect rendered_frames (slot 9 is a sibling output), so
    # prompt fields stay OUT of the render-cache hash — including the new
    # channel + global-lane state.
    scene = Scene(prompt="one", duration_frames=100)
    base = scene.content_hash()
    scene.prompt = "completely different"
    scene.prompt_sections = [PromptSection(0, 50, channels={"visual": "x", "speech": "y", "sounds": ""})]
    scene.prompt_track_config = LaneConfig(hidden=True)
    scene.global_prompt_track_config = LaneConfig(hidden=True)
    assert scene.content_hash() == base


def test_generation_job_scene_prompt_roundtrip():
    job = GenerationJob(scene_id="s1", prompt="composed", scene_prompt="global text")
    data = job.to_dict()
    assert data["scene_prompt"] == "global text"
    restored = GenerationJob.from_dict(data)
    assert restored.scene_prompt == "global text"
    # Legacy job dicts without the field default to ""
    legacy = GenerationJob.from_dict({"scene_id": "s1", "prompt": "p"})
    assert legacy.scene_prompt == ""
