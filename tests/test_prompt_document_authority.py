"""Instrument the prompt document authority from edit through frozen enqueue."""

from server import prompt_context
from server.project_manager import load_project, save_project
from server.routes import _compose_frozen_job_prompt
from server.timeline_state import GenerationJob, PromptSection, Scene, TimelineProject


def _freeze(project, section):
    job = GenerationJob(
        scene_id="scene-1",
        selection_start=0,
        selection_end=24,
        scene_prompt="",
        prompt_sections=[section.to_dict()],
        params={"snapshot_version": 1, "prompt_channel_template": "standard"},
    )
    _compose_frozen_job_prompt(project, job)
    return job.prompt


def test_deleted_prompt_text_survives_all_six_authority_layers(tmp_path):
    channel = "visual"
    original = "a woman waits beside the deleted doorway"
    edited = "a woman waits"
    section = PromptSection(
        start_frame=0,
        end_frame=24,
        channel_docs={channel: prompt_context.text_document(original, node_id="text-1")},
    )
    scene = Scene(
        scene_id="scene-1",
        duration_frames=24,
        prompt_sections=[section],
    )
    project = TimelineProject(
        project_dir=str(tmp_path / "project"),
        project_id="project-1",
        scenes=[scene],
        metadata={"prompt_channel_template": "standard"},
    )
    (tmp_path / "project").mkdir()

    # A job frozen before the edit is intentionally immutable. This rules out
    # enqueue timing before testing whether the live authority restored text.
    frozen_before_edit = _freeze(project, section)
    assert "deleted doorway" in frozen_before_edit

    # Layer 1: visible DOM/editor model. Layer 2: canonical PromptDocument.
    visible_editor_model = edited
    canonical_document = prompt_context.replace_document_text(
        section.channel_docs[channel], visible_editor_model
    )
    section.set_channel_documents({channel: canonical_document})

    # Blur/remount is a serialization boundary, not a second text authority.
    remounted = PromptSection.from_dict(section.to_dict())
    project.scenes[0].prompt_sections = [remounted]

    # Layers 3 and 4: derived flat mirror and saved/reloaded server state.
    flat_mirror = remounted.channels[channel]
    save_project(project)
    saved_project = load_project(project.project_dir)
    saved_section = saved_project.scenes[0].prompt_sections[0]
    saved_document_text = prompt_context.prompt_document_text(
        saved_section.channel_docs[channel]
    )

    # Layers 5 and 6: candidate compile and a job frozen after the edit.
    candidate = saved_project.scenes[0].compile_for_execution(
        saved_project, 0, 24, labels_on=False, template="standard", fps=24.0
    )["prompt"]
    frozen_after_edit = _freeze(saved_project, saved_section)

    layers = {
        "visible_editor_model": visible_editor_model,
        "canonical_document": prompt_context.prompt_document_text(canonical_document),
        "flat_mirror": flat_mirror,
        "saved_server_state": saved_document_text,
        "candidate_compile": candidate,
        "frozen_after_edit": frozen_after_edit,
    }
    assert all("a woman waits" in value for value in layers.values()), layers
    assert all("deleted doorway" not in value for value in layers.values()), layers
    assert candidate == frozen_after_edit == edited
    assert "deleted doorway" in frozen_before_edit
