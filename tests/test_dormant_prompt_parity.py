"""Parity and freeze contracts for candidate, execution, Relay, and dormant prompt views."""

from server import prompt_context
from server.routes import (
    _build_dormant_summary,
    _compose_frozen_job_prompt,
    _freeze_new_job_channel_template,
)
from server.timeline_state import GenerationJob, PromptSection, Scene, TimelineProject


def _project(tmp_path):
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "custom-1",
        "kind": "custom",
        "config": {"text": "edge light"},
        "capabilities": [{
            "capability_id": "prefix",
            "kind": "custom",
            "channel_key": "visual",
            "placement": "section_prefix",
        }],
    })
    section = PromptSection(
        start_frame=0,
        end_frame=24,
        channels={"visual": "a woman waits"},
        attachments=[attachment],
    )
    scene = Scene(
        scene_id="scene-1",
        name="Scene",
        duration_frames=24,
        prompt_sections=[section],
    )
    project = TimelineProject(
        project_dir=str(tmp_path / "project"),
        project_id="project-1",
        fps=24.0,
        scenes=[scene],
        metadata={"prompt_channel_template": "standard"},
    )
    project._execution_context = {
        "scene_id": "scene-1",
        "context_start": 0,
        "context_end": 24,
    }
    return project, scene, section


def test_candidate_execution_relay_and_unqueued_dormant_agree(tmp_path):
    project, scene, _section = _project(tmp_path)
    candidate = scene.compile_prompt_context(
        0, 24, labels_on=False, template="standard", fps=24.0
    )
    execution = scene.compile_for_execution(
        project, 0, 24, labels_on=False, template="standard", fps=24.0
    )
    dormant = _build_dormant_summary(project, scene_id="scene-1")

    assert candidate["prompt"] == execution["prompt"]
    assert dormant["active_scene"]["preview_prompt"] == execution["prompt"]
    assert execution["relay"]["local_prompts"] == execution["segments"][0]["text"]


def test_queued_dormant_prompt_stays_frozen_after_authored_edit(tmp_path):
    project, scene, section = _project(tmp_path)
    job = GenerationJob(
        job_id="job-1",
        status="pending",
        scene_id="scene-1",
        selection_start=0,
        selection_end=24,
        prompt_sections=[section.to_dict()],
        params={"snapshot_version": 1,
                "prompt_context_format": "prompt_context_v1"},
    )
    _freeze_new_job_channel_template(project, job)
    _compose_frozen_job_prompt(project, job)
    frozen = job.prompt
    project.generation_queue = [job]

    section.set_channels({"visual": "the authored prompt changed"})
    summary = _build_dormant_summary(project, scene_id="scene-1")

    assert summary["active_queue_job"]["preview_prompt"] == frozen
    assert "the authored prompt changed" not in frozen
    assert "the authored prompt changed" in summary["active_scene"]["preview_prompt"]


def test_attachment_free_scene_keeps_legacy_short_circuit(tmp_path, monkeypatch):
    scene = Scene(
        scene_id="scene-1",
        duration_frames=24,
        prompt_sections=[PromptSection(0, 24, channels={"visual": "plain"})],
    )
    project = TimelineProject(project_dir=str(tmp_path / "project"), scenes=[scene])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("attachment-free prompt should not invoke context compiler")

    monkeypatch.setattr(Scene, "compile_for_execution", forbidden)
    summary = _build_dormant_summary(project, scene_id="scene-1")
    assert summary["active_scene"]["preview_prompt"] == "[VISUAL]: plain"


def test_dormant_preserves_errors_to_empty_contract(tmp_path, monkeypatch):
    project, _scene, _section = _project(tmp_path)

    def blocked(self, compile_project, start, end, **kwargs):
        return {
            "prompt": "must not escape",
            "warnings": [],
            "errors": [{"code": "broken_reference_source"}],
        }

    monkeypatch.setattr(Scene, "compile_for_execution", blocked)
    summary = _build_dormant_summary(project, scene_id="scene-1")
    active = summary["active_scene"]
    assert active["preview_prompt"] == ""
    assert active["preview_prompt_degraded"] is True
    assert active["preview_prompt_error_code"] == "broken_reference_source"
