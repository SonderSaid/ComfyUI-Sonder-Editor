"""Tests for lightweight dormant summary helpers."""

from server.routes import _build_dormant_summary, _build_selection_summary
from server.timeline_state import (
    Asset,
    AudioTrack,
    ClipReference,
    GenerationJob,
    GuideFrame,
    PromptSection,
    Scene,
    TimelineProject,
)


def _make_project():
    scene = Scene(
        scene_id="scene-1",
        name="Opening",
        order=1,
        duration_frames=120,
        width=1024,
        height=576,
        fps=30.0,
    )
    scene.clips = [
        ClipReference(
            clip_id="clip-a",
            source_path="media/a.mp4",
            timeline_start_frame=0,
            timeline_end_frame=60,
            track_index=0,
        ),
        ClipReference(
            clip_id="clip-b",
            source_path="media/b.mp4",
            timeline_start_frame=20,
            timeline_end_frame=90,
            track_index=1,
        ),
    ]
    scene.audio_tracks = [
        AudioTrack(
            track_id="audio-a",
            source_path="media/a.wav",
            timeline_start_frame=0,
            timeline_end_frame=120,
            lane_index=0,
        )
    ]
    scene.guide_frames = [GuideFrame(frame_index=48, asset_id="img-1")]
    scene.prompt_sections = [PromptSection(start_frame=0, end_frame=60, prompt="test")]

    project = TimelineProject(
        project_dir="/tmp/test-project",
        project_id="project-1",
        name="Dormant Test",
        fps=24.0,
        resolution=(768, 512),
        scenes=[scene],
        assets=[
            Asset(asset_id="vid-1", name="A", asset_type="video", path="media/a.mp4"),
            Asset(asset_id="vid-2", name="B", asset_type="video", path="media/b.mp4"),
            Asset(asset_id="img-1", name="Guide", asset_type="image", path="media/guide.png"),
            Asset(asset_id="aud-1", name="Audio", asset_type="audio", path="media/a.wav"),
        ],
        generation_queue=[
            GenerationJob(
                job_id="job-1",
                status="pending",
                scene_id="scene-1",
                scene_name="Opening",
                selection_start=12,
                selection_end=36,
            ),
            GenerationJob(
                job_id="job-2",
                status="running",
                scene_id="scene-1",
                scene_name="Opening",
                selection_start=24,
                selection_end=72,
                pre_context_frames=4,
                post_context_frames=8,
                scene_width=1280,
                scene_height=720,
                scene_fps=24.0,
            ),
            GenerationJob(job_id="job-3", status="completed", scene_name="Opening"),
        ],
    )
    return project


def test_build_selection_summary_full_scene_defaults():
    scene = Scene(scene_id="scene-1", duration_frames=48)

    summary = _build_selection_summary(scene)

    assert summary["is_full_scene"] is True
    assert summary["generation_start_frame"] == 0
    assert summary["generation_end_frame"] == 48
    assert summary["context_start_frame"] == 0
    assert summary["context_end_frame"] == 48
    assert summary["frame_count"] == 48
    assert summary["label"] == "Full Scene (48f)"


def test_build_selection_summary_clamps_context_to_scene_bounds():
    scene = Scene(scene_id="scene-1", duration_frames=100)

    summary = _build_selection_summary(
        scene,
        selection_start=10,
        selection_end=20,
        pre_context_frames=25,
        post_context_frames=90,
    )

    assert summary["is_full_scene"] is False
    assert summary["generation_start_frame"] == 10
    assert summary["generation_end_frame"] == 20
    assert summary["context_start_frame"] == 0
    assert summary["context_end_frame"] == 100
    assert summary["pre_context_frames"] == 10
    assert summary["post_context_frames"] == 80
    assert summary["frame_count"] == 100


def test_build_dormant_summary_reports_counts_and_effective_scene_values():
    project = _make_project()

    summary = _build_dormant_summary(
        project,
        scene_id="scene-1",
        selection_start=24,
        selection_end=72,
        pre_context_frames=8,
        post_context_frames=16,
    )

    assert summary["name"] == "Dormant Test"
    assert summary["scene_count"] == 1
    assert summary["asset_counts"] == {"video": 2, "image": 1, "audio": 1, "artifact": 0, "total": 4}
    assert summary["queue_counts"] == {
        "pending": 1,
        "running": 1,
        "completed": 1,
        "failed": 0,
        "total": 3,
    }
    assert summary["active_queue_job"] == {
        "job_id": "job-2",
        "status": "running",
        "scene_id": "scene-1",
        "scene_name": "Opening",
        "selection_start": 24,
        "selection_end": 72,
        "context_frames": 0,
        "pre_context_frames": 4,
        "post_context_frames": 8,
        "scene_width": 1280,
        "scene_height": 720,
        "scene_fps": 24.0,
        "preview_prompt": "",
    }

    active = summary["active_scene"]
    assert active["scene_id"] == "scene-1"
    assert active["name"] == "Opening"
    assert active["effective_width"] == 1024
    assert active["effective_height"] == 576
    assert active["effective_fps"] == 30.0
    assert active["clip_count"] == 2
    assert active["audio_track_count"] == 1
    assert active["guide_count"] == 1
    assert active["prompt_section_count"] == 1
    assert active["selection"]["context_start_frame"] == 16
    assert active["selection"]["context_end_frame"] == 88
    assert active["selection"]["frame_count"] == 72


def test_build_dormant_summary_falls_back_to_first_scene_when_scene_id_missing():
    project = _make_project()

    summary = _build_dormant_summary(project, scene_id="missing")

    assert summary["active_scene"]["scene_id"] == "scene-1"


def test_build_dormant_summary_uses_pending_when_no_running_queue_job():
    project = _make_project()
    project.generation_queue[1].status = "completed"

    summary = _build_dormant_summary(project, scene_id="scene-1")

    assert summary["active_queue_job"]["job_id"] == "job-1"
    assert summary["active_queue_job"]["status"] == "pending"
    assert summary["active_queue_job"]["selection_start"] == 12
    assert summary["active_queue_job"]["selection_end"] == 36


def test_build_dormant_summary_ignores_completed_and_failed_queue_jobs():
    project = _make_project()
    for job in project.generation_queue:
        job.status = "failed" if job.job_id == "job-1" else "completed"

    summary = _build_dormant_summary(project, scene_id="scene-1")

    assert summary["active_queue_job"] is None


def test_dormant_queue_job_preview_prompt_is_frozen_verbatim():
    # Snapshot jobs carry their frozen prompt VERBATIM — after the server-side
    # enqueue compose this IS the executed slot-9 string; recomposing here
    # would lie for pre-upgrade jobs (audit F4)
    project = _make_project()
    project.generation_queue[1].prompt = "frozen executed string"

    summary = _build_dormant_summary(project, scene_id="scene-1")

    assert summary["active_queue_job"]["preview_prompt"] == "frozen executed string"


def test_dormant_live_preview_prompt_over_context_window():
    project = _make_project()
    project.metadata["prompt_section_delimiter"] = ","
    scene = project.scenes[0]
    scene.prompt = "global"
    scene.prompt_sections = [
        PromptSection(start_frame=0, end_frame=40, prompt="first"),
        PromptSection(start_frame=40, end_frame=120, prompt="second"),
    ]

    # Selection 30-50 with pre/post 10 → context window [20, 60) spans both
    summary = _build_dormant_summary(
        project, scene_id="scene-1",
        selection_start=30, selection_end=50,
        pre_context_frames=10, post_context_frames=10,
    )
    assert summary["active_scene"]["preview_prompt"] == (
        "global first, second"
    )

    # Empty selection params → full-scene fallback compose
    summary = _build_dormant_summary(project, scene_id="scene-1")
    assert summary["active_scene"]["preview_prompt"] == (
        "global first, second"
    )

    # Hidden lanes are honored on the live branch
    scene.prompt_track_config.hidden = True
    summary = _build_dormant_summary(project, scene_id="scene-1")
    assert summary["active_scene"]["preview_prompt"] == "global"
