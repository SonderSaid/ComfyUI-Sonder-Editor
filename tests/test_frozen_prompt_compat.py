"""Frozen prompt format gates and reviewed v0.2.2 golden vectors."""

import json
from pathlib import Path

import pytest

from server import frozen_prompt, prompt_channel_templates, routes
from server.timeline_state import GenerationJob, Scene, TimelineProject


FIXTURES = Path(__file__).parent


def _job(*, start=0, end=20, global_prompt="", sections=None, params=None):
    return GenerationJob(
        scene_id="scene", selection_start=start, selection_end=end,
        scene_prompt=global_prompt, prompt_sections=sections or [],
        params={"snapshot_version": 1, **(params or {})})


@pytest.mark.parametrize(("project", "job", "expected_prompt", "expected_relay"), [
    (
        TimelineProject(project_id="p", metadata={
            "prompt_channel_labels": False,
            "prompt_section_delimiter": ",",
            "prompt_frame_threshold": 10,
        }),
        _job(start=5, end=65, global_prompt="global", sections=[
            {"start_frame": 0, "end_frame": 30,
             "channels": {"visual": "first", "speech": "", "sounds": ""}},
            {"start_frame": 30, "end_frame": 80,
             "channels": {"visual": "second", "speech": "", "sounds": ""}},
        ]),
        "global [VISUAL]: first, second",
        {
            "global_prompt": "global",
            "smart_prompt": "first [0-25] | second [25-60]",
            "local_prompts": "first | second",
            "segment_lengths": "25,35",
            "segments": [
                {"text": "first", "start": 0, "end": 25},
                {"text": "second", "start": 25, "end": 60},
            ],
            "labels_on": False, "window_start": 5, "window_end": 65,
            "source": "snapshot",
        },
    ),
    (
        TimelineProject(project_id="p", metadata={
            "prompt_channel_labels": True,
            "prompt_section_delimiter": " | ",
            "prompt_frame_threshold": 0,
        }),
        _job(global_prompt="global", sections=[
            {"start_frame": 0, "end_frame": 10,
             "channels": {"visual": "first", "speech": "hello", "sounds": ""}},
            {"start_frame": 10, "end_frame": 20,
             "channels": {"visual": "second", "speech": "", "sounds": "rain"}},
        ]),
        "global [VISUAL]: first| second [SPEECH]: hello [SOUNDS]: rain",
        {
            "global_prompt": "global",
            "smart_prompt": ("[VISUAL]: first [SPEECH]: hello [0-10] | "
                             "[VISUAL]: second [SOUNDS]: rain [10-20]"),
            "local_prompts": ("[VISUAL]: first [SPEECH]: hello | "
                              "[VISUAL]: second [SOUNDS]: rain"),
            "segment_lengths": "10,10",
            "segments": [
                {"text": "[VISUAL]: first [SPEECH]: hello", "start": 0, "end": 10},
                {"text": "[VISUAL]: second [SOUNDS]: rain", "start": 10, "end": 20},
            ],
            "labels_on": True, "window_start": 0, "window_end": 20,
            "source": "snapshot",
        },
    ),
    (
        TimelineProject(project_id="p", metadata={"prompt_frame_threshold": 10}),
        _job(start=0, end=42, sections=[
            {"start_frame": 0, "end_frame": 40, "channels": {"visual": "a"}},
            {"start_frame": 40, "end_frame": 80, "channels": {"visual": "b"}},
        ]),
        "[VISUAL]: a",
        {
            "global_prompt": "", "smart_prompt": "a [0-42]",
            "local_prompts": "a", "segment_lengths": "42",
            "segments": [{"text": "a", "start": 0, "end": 42}],
            "labels_on": False, "window_start": 0, "window_end": 42,
            "source": "snapshot",
        },
    ),
])
def test_marker_absent_v022_matches_reviewed_origin_goldens(
        project, job, expected_prompt, expected_relay):
    frozen_prompt.compose_v022_job_prompt(project, job)
    assert job.prompt == expected_prompt
    assert frozen_prompt.build_v022_relay_payload(
        job, job.selection_start, job.selection_end) == expected_relay


def test_v022_recompose_never_reads_live_scene_or_changed_metadata():
    project = TimelineProject(
        project_id="p", metadata={"prompt_section_delimiter": ","},
        scenes=[Scene(scene_id="scene", prompt="live scene text")])
    job = _job(global_prompt="frozen", sections=[
        {"start_frame": 0, "end_frame": 10, "channels": {"visual": "one"}},
        {"start_frame": 10, "end_frame": 20, "channels": {"visual": "two"}},
    ])
    frozen_prompt.compose_v022_job_prompt(project, job)
    expected = job.prompt
    project.scenes[0].prompt = "mutated live prompt"
    project.metadata["prompt_section_delimiter"] = " / "
    frozen_prompt.compose_v022_job_prompt(project, job)
    assert job.prompt == expected
    project.scenes.clear()
    frozen_prompt.compose_v022_job_prompt(project, job)
    assert job.prompt == expected


@pytest.mark.parametrize("marker", ["prompt_context_v2", "garbage", 2])
def test_unknown_frozen_prompt_format_has_controlled_diagnostic(marker):
    job = _job(params={"prompt_context_format": marker})
    with pytest.raises(frozen_prompt.FrozenPromptEnvelopeError) as exc:
        frozen_prompt.classify_frozen_prompt(job)
    assert exc.value.code == "unsupported_prompt_context_format"


@pytest.mark.parametrize("field,value", [
    ("compiled_prompt_context", {"prompt": "new"}),
    ("reference_lane_count", 2),
    ("reference_item_snapshots", [{"reference_item_id": "new"}]),
])
def test_unmarked_post_v022_job_state_is_never_guessed(field, value):
    job = _job()
    setattr(job, field, value)
    with pytest.raises(frozen_prompt.FrozenPromptEnvelopeError) as exc:
        frozen_prompt.classify_frozen_prompt(job)
    assert exc.value.code == "unmarked_prompt_context_envelope"


def test_marker_absent_raw_queue_allowlist_rejects_new_fields_before_drop():
    with pytest.raises(frozen_prompt.FrozenPromptEnvelopeError) as exc:
        frozen_prompt.validate_marker_absent_queue_body(
            {"scene_id": "scene", "compiled_prompt_context": {}},
            {"snapshot_version": 1})
    assert exc.value.code == "unmarked_prompt_context_envelope"


def test_prompt_context_v1_requires_complete_template_and_scene():
    project = TimelineProject(project_id="p", scenes=[Scene(scene_id="scene")])
    job = _job(params={"prompt_context_format": "prompt_context_v1"})
    with pytest.raises(routes.ProjectMutationRequestError) as exc:
        routes._compose_frozen_job_prompt(project, job)
    assert exc.value.code == "invalid_frozen_prompt_template"

    job.params["prompt_channel_template"] = (
        prompt_channel_templates.template_freeze_value("standard"))
    project.scenes.clear()
    with pytest.raises(routes.ProjectMutationRequestError) as exc:
        routes._compose_frozen_job_prompt(project, job)
    assert exc.value.code == "prompt_context_scene_missing"


def test_representative_v022_project_and_job_semantically_round_trip():
    project_raw = json.loads(
        (FIXTURES / "fixture_v022_project.json").read_text(encoding="utf-8"))
    project = TimelineProject.from_dict(project_raw)
    saved = project.to_dict()
    assert saved["project_id"] == "v022-project"
    assert saved["metadata"]["published_unknown_setting"] == {"keep": "opaque"}
    assert saved["scenes"][0]["video_lane_count"] == 18
    assert len(saved["scenes"][0]["video_lane_configs"]) == 18
    assert saved["scenes"][0]["prompt_sections"][0]["channels"][
        "vendor_channel"] == "preserve authored unknown channel"
    assert "future_top_level_blob" not in saved

    job_raw = json.loads(
        (FIXTURES / "fixture_v022_job.json").read_text(encoding="utf-8"))
    job = GenerationJob.from_dict(job_raw)
    assert frozen_prompt.classify_frozen_prompt(job) == "v0.2.2"
    assert GenerationJob.from_dict(job.to_dict()).prompt_sections == job.prompt_sections
