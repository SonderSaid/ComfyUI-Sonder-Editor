"""Tests for timeline state data types — serialization roundtrips."""

import sys
import os

# Add project root to path so we can import without ComfyUI
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.timeline_state import (
    Asset, GuideFrame, BatchConfig, Scene, PromptSection,
    ClipReference, AudioTrack, GenerationJob, TimelineProject,
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


# --- GuideFrame ---

def test_guide_frame_roundtrip():
    gf = GuideFrame(
        frame_index=123,
        asset_id="img001",
        source="asset",
        strength=0.8,
    )
    data = gf.to_dict()
    restored = GuideFrame.from_dict(data)

    assert restored.frame_index == 123
    assert restored.asset_id == "img001"
    assert restored.source == "asset"
    assert restored.strength == 0.8


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
        GuideFrame(frame_index=0, asset_id="img001", source="asset"),
        GuideFrame(frame_index=-1, asset_id="img002", source="asset"),
    ]
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


# --- ClipReference (unchanged) ---

def test_clip_reference_roundtrip():
    clip = ClipReference(
        clip_id="abc123",
        source_path="/path/to/video.mp4",
        timeline_start_frame=0,
        timeline_end_frame=120,
        source_in_frame=10,
        source_out_frame=130,
        track_index=0,
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
    assert restored.prompt == clip.prompt
    assert restored.is_generated == clip.is_generated
    assert restored.generation_params == clip.generation_params
    assert restored.takes == clip.takes
    assert restored.active_take == clip.active_take


def test_clip_reference_duration():
    clip = ClipReference(timeline_start_frame=10, timeline_end_frame=50)
    assert clip.duration_frames == 40


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
        status="running",
        params={"steps": 20},
        progress=0.5,
    )

    data = job.to_dict()
    restored = GenerationJob.from_dict(data)

    assert restored.job_id == job.job_id
    assert restored.scene_id == "sc001"
    assert restored.batch_index == 2
    assert restored.status == "running"
    assert restored.progress == 0.5


# --- TimelineProject ---

def test_timeline_project_roundtrip():
    project = TimelineProject(
        project_dir="/tmp/test_project",
        project_id="proj123",
        name="Test Project",
        fps=30.0,
        resolution=(1920, 1080),
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
    data = ps.to_dict()
    restored = PromptSection.from_dict(data)
    assert restored.start_frame == 0
    assert restored.end_frame == 100
    assert restored.prompt == "girl feeds dog"


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
        prompt="fallback",
        prompt_sections=[
            PromptSection(start_frame=0, end_frame=100, prompt="section A"),
            PromptSection(start_frame=100, end_frame=200, prompt="section B"),
        ],
    )

    assert scene.get_prompt_at_frame(0) == "section A"
    assert scene.get_prompt_at_frame(50) == "section A"
    assert scene.get_prompt_at_frame(99) == "section A"
    assert scene.get_prompt_at_frame(100) == "section B"
    assert scene.get_prompt_at_frame(150) == "section B"
    # Frame outside all sections falls back to scene prompt
    assert scene.get_prompt_at_frame(200) == "fallback"
    assert scene.get_prompt_at_frame(999) == "fallback"


def test_scene_get_prompt_for_range():
    scene = Scene(
        prompt="fallback",
        prompt_sections=[
            PromptSection(start_frame=0, end_frame=100, prompt="section A"),
            PromptSection(start_frame=100, end_frame=200, prompt="section B"),
        ],
    )

    # Range fully within section A
    assert scene.get_prompt_for_range(0, 100) == "section A"
    # Range fully within section B
    assert scene.get_prompt_for_range(100, 200) == "section B"
    # Range overlapping both — returns first matching
    assert scene.get_prompt_for_range(50, 150) == "section A"
    # Range outside all sections
    assert scene.get_prompt_for_range(200, 300) == "fallback"


def test_scene_no_prompt_sections_uses_fallback():
    scene = Scene(prompt="the only prompt", prompt_sections=[])
    assert scene.get_prompt_at_frame(0) == "the only prompt"
    assert scene.get_prompt_for_range(0, 100) == "the only prompt"
