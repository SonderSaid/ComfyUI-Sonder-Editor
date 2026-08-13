"""Regression coverage for the second Reference/Prompt authoring practice run."""

import pytest

from server import prompt_channel_templates, prompt_context, prompt_live_context, routes
from server.timeline_state import LaneConfig, PromptSection, Scene, TimelineProject


def _section(start, end, text, attachments=()):
    return PromptSection(start, end, channels={"visual": text},
                         attachments=list(attachments))


def _reference_context():
    return {
        "ordinal_manifest": {"subjects": {"a": 1}},
        "unit_source_labels": {"a": ["<Picture 1>"]},
        "semantic_units": [{
            "semantic_unit_id": "a", "name": "A", "definition": "a woman",
        }],
    }


def _linked_reference(attachment_id, capabilities, *, group="linked", config=None):
    return prompt_context.normalize_attachment({
        "attachment_id": attachment_id,
        "emission_group_id": group,
        "kind": "reference",
        "source": {"semantic_unit_ids": ["a"]},
        "config": config or {"text": "<Subject 1>"},
        "capabilities": capabilities,
    })


def test_shared_live_compiler_is_the_scene_and_routes_authority():
    scene = Scene(scene_id="scene", duration_frames=24)
    scene.prompt_track_config = LaneConfig()
    scene.global_prompt_track_config = LaneConfig()
    scene.prompt_sections = [_section(0, 24, "a dog walks")]
    project = TimelineProject(project_id="project", scenes=[scene])
    template = prompt_channel_templates.get_channel_template("standard")

    direct = prompt_live_context.compile_live_scene_prompt_context(
        project, scene, template=template, window_start=0, window_end=24,
        fps=24, labels_on=False)
    through_scene = scene.compile_for_execution(
        project, 0, 24, template=template, fps=24, labels_on=False)

    assert routes.compile_live_scene_prompt_context is (
        prompt_live_context.compile_live_scene_prompt_context)
    assert routes.resolve_scene_prompt_context is (
        prompt_live_context.resolve_scene_prompt_context)
    assert not hasattr(routes, "_compile_live_scene_prompt_context")
    assert not hasattr(routes, "_resolve_scene_prompt_context")
    assert through_scene == direct
    assert scene.get_prompt_for_range(
        0, 24, project=project, template=template, fps=24,
        labels_on=False) == direct["prompt"]


@pytest.mark.parametrize("scene_present", [False, True])
def test_preview_only_channel_routes_are_stripped_at_both_freeze_sites(scene_present):
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "routed", "kind": "custom", "config": {"text": "EDGE"},
        "capabilities": [{"capability_id": "prefix", "kind": "custom",
                          "channel_key": "visual", "placement": "section_prefix"}],
    })
    section = _section(0, 10, "BODY", [attachment])
    scene = Scene(scene_id="scene", duration_frames=10,
                  prompt_track_config=LaneConfig(), global_prompt_track_config=LaneConfig(),
                  prompt_sections=[section])
    project = TimelineProject(project_id="project", fps=24,
                              scenes=[scene] if scene_present else [],
                              metadata={"prompt_channel_template": "standard"})
    job = routes.GenerationJob(
        scene_id="scene", selection_start=0, selection_end=10,
        prompt_sections=[section.to_dict()],
        params={"snapshot_version": 1,
                "prompt_context_format": "prompt_context_v1"})
    routes._freeze_new_job_channel_template(project, job)
    expected = prompt_context.compile_prompt_context(
        global_channels={}, sections=[section], window_start=0, window_end=10,
        fps=24, template="standard", boundary_threshold_pct=10.0)
    assert "attachment_channel_routes" in expected
    assert "attachment_capability_projections" in expected

    if not scene_present:
        with pytest.raises(routes.ProjectMutationRequestError) as refused:
            routes._compose_frozen_job_prompt(project, job)
        assert refused.value.code == "prompt_context_scene_missing"
        return
    routes._compose_frozen_job_prompt(project, job)

    assert "attachment_channel_routes" not in job.compiled_prompt_context
    assert "attachment_capability_projections" not in job.compiled_prompt_context
    assert job.compiled_prompt_context["content_hash"] == expected["content_hash"]


def test_section_global_document_is_rendered_and_multi_placement_routes_survive():
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "multi-placement", "kind": "custom",
        "config": {"text": "EDGE"},
        "capabilities": [
            {"capability_id": "global", "kind": "custom",
             "channel_key": "visual", "placement": "global_document"},
            {"capability_id": "suffix", "kind": "custom",
             "channel_key": "visual", "placement": "section_suffix"},
        ],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "BODY", [attachment])],
        window_start=0, window_end=10, fps=24, template="standard")

    assert compiled["prompt"] == "EDGE BODY EDGE"
    assert [value["placement"] for value in compiled["emissions"]] == [
        "global_document", "section_suffix"]
    assert compiled["attachment_channel_routes"]["multi-placement"]["visual"] == [
        "global_document", "section_suffix"]


def test_standalone_time_survives_round_trips_and_does_not_mint_a_shot():
    channel = "integrated_multimodal_description"
    time = prompt_context.timestamp_attachment(attachment_id="time")
    first = PromptSection(0, 120, channels={channel: "a dog walks"})
    second = PromptSection(120, 240, channels={channel: "it barks"},
                           attachments=[time])
    restored = PromptSection.from_dict(second.to_dict())

    assert [value["kind"] for value in restored.attachments] == ["timestamp"]
    assert restored.attachments[0]["config"]["standalone"] is True
    assert "starts_new_shot" not in restored.to_dict()
    assert "shot_timestamp" not in restored.to_dict()

    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[first, restored], window_start=0,
        window_end=240, fps=24,
        template=prompt_channel_templates.get_channel_template("minimax_h3_base"))
    assert "At 00:05.000, it barks" in compiled["prompt"]
    assert "[Shot" not in compiled["prompt"]
    assert compiled["attachment_channel_routes"]["time"] == {
        channel: "section_prefix"}
    assert compiled["attachment_channel_previews"]["time"] == {
        channel: "At 00:05.000,"}
    assert compiled["attachment_previews"]["time"] == "At 00:05.000,"
    routes._validate_prompt_identity(restored, restored.to_dict())


def test_standalone_time_combines_with_existing_shot_numbering_and_is_not_split():
    channel = "integrated_multimodal_description"
    first = PromptSection(0, 120, channels={channel: "one"},
                          attachments=[prompt_context.shot_attachment()])
    second = PromptSection(120, 240, channels={channel: "two"}, attachments=[
        prompt_context.shot_attachment(),
        prompt_context.timestamp_attachment(attachment_id="time"),
    ])
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[first, second], window_start=0,
        window_end=240, fps=24,
        template=prompt_channel_templates.get_channel_template("minimax_h3_base"))
    assert "[Shot 2] At 00:05.000, two" in compiled["prompt"]

    scene = Scene(scene_id="scene", duration_frames=240,
                  prompt_sections=[second], prompt_track_config=LaneConfig())
    right = routes._split_prompt_object(scene, second, 180)
    assert all(value["kind"] != "timestamp" for value in right.attachments)


def test_linked_reference_definition_dedupes_but_mentions_emit_per_placement():
    capabilities = [
        {"capability_id": "definitions", "kind": "definitions",
         "placement": "section_prefix", "channel_key": "visual"},
        {"capability_id": "mentions", "kind": "mentions",
         "placement": "section_prefix", "channel_key": "visual"},
    ]
    first = _linked_reference("a", capabilities)
    second = _linked_reference("b", capabilities)
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "one", [first]),
                                      _section(10, 20, "two", [second])],
        window_start=0, window_end=20, fps=24,
        template=prompt_channel_templates.DEFAULT_CHANNEL_TEMPLATE_ID,
        context=_reference_context())

    emissions = compiled["emissions"]
    assert len([value for value in emissions
                if value["capability_id"] == "definitions"]) == 1
    assert len([value for value in emissions
                if value["capability_id"] == "mentions"]) == 2
    assert compiled["errors"] == []


def test_split_derived_reference_clone_keeps_group_with_fresh_record_identity():
    source = _linked_reference("before-split", [{
        "capability_id": "definitions", "kind": "definitions",
        "placement": "section_prefix", "channel_key": "visual",
    }], group="split-group")
    section = _section(0, 20, "one", [source])
    scene = Scene(scene_id="scene", duration_frames=20,
                  prompt_track_config=LaneConfig(), prompt_sections=[section])

    right = routes._split_prompt_object(scene, section, 10)

    assert section.end_frame == 10
    assert right.start_frame == 10
    assert right.attachments[0]["attachment_id"] != source["attachment_id"]
    assert right.attachments[0]["emission_group_id"] == source["emission_group_id"]
    assert right.attachments[0]["source"] == source["source"]
    assert right.attachments[0]["config"] == source["config"]
    assert right.attachments[0]["capabilities"] == source["capabilities"]


def test_linked_reference_routes_different_channels_and_accumulates_shots():
    visual_definition = _linked_reference("route-a", [{
        "capability_id": "definitions", "kind": "definitions",
        "placement": "section_prefix", "channel_key": "visual",
    }])
    speech_definition = _linked_reference("route-b", [{
        "capability_id": "definitions", "kind": "definitions",
        "placement": "section_prefix", "channel_key": "speech",
    }])
    routed = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[_section(0, 10, "one", [visual_definition]),
                  _section(10, 20, "two", [speech_definition])],
        window_start=0, window_end=20, fps=24,
        template=prompt_channel_templates.DEFAULT_CHANNEL_TEMPLATE_ID,
        context=_reference_context())
    assert {value["channel_key"] for value in routed["emissions"]} == {
        "visual", "speech"}

    retention_capability = [{
        "capability_id": "retention", "kind": "retention",
        "placement": "section_prefix", "channel_key": "visual",
    }]
    visual = _linked_reference("a", retention_capability,
                               config={"retention_detail": "keep face"})
    second_visual = _linked_reference("b", retention_capability,
                                      config={"retention_detail": "keep face"})
    shot_a = prompt_context.shot_attachment(attachment_id="shot-a")
    shot_b = prompt_context.shot_attachment(attachment_id="shot-b")
    compiled = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[_section(0, 10, "one", [shot_a, visual]),
                  _section(10, 20, "two", [shot_b, second_visual])],
        window_start=0, window_end=20, fps=24, template="standard",
        context=_reference_context())

    assert "appears in [Shot 1], [Shot 2]" in compiled["prompt"]


def test_unlinked_divergent_definitions_conflict_and_deleting_one_leaves_other():
    capability = [{
        "capability_id": "definitions", "kind": "definitions",
        "placement": "section_prefix", "channel_key": "visual",
    }]
    red = _linked_reference("a", capability, group="one",
                            config={"definition": "a woman in red"})
    blue = _linked_reference("b", capability, group="two",
                             config={"definition": "a woman in blue"})
    conflicted = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "one", [red]),
                                      _section(10, 20, "two", [blue])],
        window_start=0, window_end=20, fps=24, template="standard",
        context=_reference_context())
    assert any(value["code"] == "conflicting_emission"
               for value in conflicted["errors"])

    remaining = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "one", [red]),
                                      _section(10, 20, "two", [])],
        window_start=0, window_end=20, fps=24, template="standard",
        context=_reference_context())
    assert not any(value["code"] == "conflicting_emission"
                   for value in remaining["errors"])


def test_linked_batch_stale_snapshot_refuses_before_any_member_changes(monkeypatch):
    original_a = prompt_context.normalize_attachment({
        "attachment_id": "a", "emission_group_id": "linked", "kind": "custom",
        "config": {"text": "before"},
    })
    original_b = prompt_context.normalize_attachment({
        "attachment_id": "b", "emission_group_id": "linked", "kind": "custom",
        "config": {"text": "before"},
    })
    scene = Scene(scene_id="scene", duration_frames=20,
                  prompt_track_config=LaneConfig(), prompt_sections=[
                      _section(0, 10, "one", [original_a]),
                      _section(10, 20, "two", [original_b]),
                  ])
    project = TimelineProject(project_id="project", scenes=[scene])
    changed_a = prompt_context.normalize_attachment({
        **original_a, "config": {"text": "after"}})
    changed_b = prompt_context.normalize_attachment({
        **original_b, "config": {"text": "after"}})
    operations = [
        {"type": "update_prompt_section", "index": 0,
         "expected": scene.prompt_sections[0].to_dict(),
         "fields": {"attachments": [changed_a]}},
        {"type": "update_prompt_section", "index": 1,
         "expected": {**scene.prompt_sections[1].to_dict(), "attachments": []},
         "fields": {"attachments": [changed_b]}},
    ]
    monkeypatch.setattr(routes, "_load_project_from_request", lambda _request: project)
    monkeypatch.setattr(routes, "save_project",
                        lambda _project: pytest.fail("stale batch was saved"))

    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_scene_mutations_sync(object(), "scene", operations)
    assert refused.value.code == "identity_mismatch"
    assert scene.prompt_sections[0].attachments[0]["config"]["text"] == "before"
    assert scene.prompt_sections[1].attachments[0]["config"]["text"] == "before"


def test_generated_subject_payload_names_its_owning_reference():
    from server.timeline_state import ReferenceEntity

    project = TimelineProject(project_id="project")
    project.references = [ReferenceEntity(reference_id="ref", name="Granny")]
    project.prompt_semantic_units = [{
        "semantic_unit_id": "unit:ref", "name": "Granny",
        "source_members": [],
    }]
    unit = routes._references_payload(project)["prompt_semantic_units"][0]
    assert unit["generated"] is True
    assert unit["generated_reference_name"] == "Granny"
