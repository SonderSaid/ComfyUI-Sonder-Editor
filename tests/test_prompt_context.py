import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server import minimax_h3, prompt_context, routes
from server.timeline_state import (
    Asset,
    GenerationJob,
    GuideFrame,
    PromptSection,
    ReferenceEntity,
    ReferenceItem,
    ReferenceLaneRecipe,
    ReferenceMember,
    Scene,
    TimelineProject,
)


def _section(start, end, text, *, shot=False, timestamp=False,
             prompt_id=None, attachments=None, document=None):
    authored_attachments = list(attachments or [])
    if shot:
        authored_attachments.insert(0, prompt_context.shot_attachment(
            timestamp=timestamp))
    elif timestamp:
        authored_attachments.insert(0, prompt_context.timestamp_attachment())
    section = PromptSection(
        start_frame=start, end_frame=end, channels={"visual": text},
        attachments=authored_attachments,
        channel_docs={"visual": document} if document else None,
    )
    if prompt_id:
        section.prompt_id = prompt_id
    return section


@pytest.mark.parametrize(
    "shot,timestamp,prefix",
    [
        (False, False, "plain"),
        (True, False, "[Shot 1] plain"),
        (True, True, "[Shot 1] At 00:00.000, plain"),
    ],
)
def test_shot_timestamp_option_round_trips_and_compiles(
        shot, timestamp, prefix):
    section = _section(0, 24, "plain", shot=shot, timestamp=timestamp)
    restored = PromptSection.from_dict(section.to_dict())
    marker = next((value for value in restored.attachments
                   if value["kind"] in {"shot", "timestamp"}), None)
    assert (marker is not None) is (shot or timestamp)
    if marker:
        assert marker["kind"] == ("shot" if shot else "timestamp")
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[restored], window_start=0,
        window_end=24, fps=24, template="standard")
    assert compiled["prompt"] == prefix


def test_removed_marker_mirrors_do_not_migrate_into_authored_state():
    restored = PromptSection.from_dict({
        "start_frame": 0, "end_frame": 24,
        "channels": {"visual": "plain"},
        "starts_new_shot": False, "shot_timestamp": True,
    })
    assert restored.attachments == []
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[restored], window_start=0,
        window_end=24, fps=24, template="standard")
    assert compiled["prompt"] == "plain"


def test_flat_document_edit_refuses_to_delete_inline_anchor():
    attachment = prompt_context.normalize_attachment({
        "kind": "custom", "config": {"text": "anchor"}})
    document = {"nodes": [
        {"type": "text", "node_id": "a", "text": "before "},
        {"type": "attachment", "node_id": "b",
         "attachment_id": attachment["attachment_id"]},
    ]}
    section = _section(0, 10, "", attachments=[attachment], document=document)
    with pytest.raises(ValueError, match="structured_edit_conflict"):
        section.set_channels({"visual": "replacement"})


def test_enabled_guide_is_preserved_and_blocks_as_unsupported():
    attachment = prompt_context.normalize_attachment({
        "kind": "guide", "config": {"text": "at the midpoint"}})
    document = {"nodes": [
        {"type": "text", "node_id": "a", "text": "Move "},
        {"type": "attachment", "node_id": "b",
         "attachment_id": attachment["attachment_id"]},
        {"type": "text", "node_id": "c", "text": " slowly."},
    ]}
    section = _section(0, 10, "", attachments=[attachment], document=document)
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[section], window_start=0,
        window_end=10, fps=24, template="standard")
    assert any(value["code"] == "unsupported_attachment_kind"
               and value["attachment_id"] == attachment["attachment_id"]
               for value in compiled["errors"])
    assert section.to_dict()["attachments"][0]["kind"] == "guide"


def _link_attachment(source_id):
    return prompt_context.normalize_attachment({
        "kind": "prompt_link",
        "source": {"prompt_id": source_id, "channel_key": "visual"},
    })


def _scope_link_attachment(source_id, channels=None):
    return prompt_context.normalize_attachment({
        "kind": "prompt_link_scope",
        "source": {"prompt_id": source_id, "channel_keys": channels or ["visual"]},
    })


def test_prompt_link_materializes_only_when_source_is_absent():
    source = _section(0, 10, "creaky footsteps", prompt_id="source")
    link = _link_attachment("source")
    document = {"nodes": [
        {"type": "attachment", "node_id": "link",
         "attachment_id": link["attachment_id"]},
        {"type": "text", "node_id": "text", "text": ", then a thump"},
    ]}
    consumer = _section(10, 20, "", prompt_id="consumer",
                        attachments=[link], document=document)

    combined = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, consumer], window_start=0,
        window_end=20, fps=24, template="standard")
    assert combined["prompt"] == "creaky footsteps. , then a thump"

    consumer_only = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, consumer], window_start=10,
        window_end=20, fps=24, template="standard")
    assert consumer_only["prompt"] == "creaky footsteps, then a thump"


def test_prompt_link_rejects_later_source():
    link = _link_attachment("later")
    document = {"nodes": [{"type": "attachment", "node_id": "link",
                            "attachment_id": link["attachment_id"]}]}
    consumer = _section(0, 10, "", prompt_id="consumer",
                        attachments=[link], document=document)
    later = _section(10, 20, "later", prompt_id="later")
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[consumer, later], window_start=0,
        window_end=10, fps=24, template="standard")
    assert {value["code"] for value in compiled["errors"]} == {
        "invalid_prompt_link_order"}
    assert compiled["errors"][0]["attachment_id"] == link["attachment_id"]


def test_broken_prompt_link_diagnostic_targets_the_link_chip():
    link = _link_attachment("missing")
    document = {"nodes": [{"type": "attachment", "node_id": "link",
                            "attachment_id": link["attachment_id"]}]}
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(
            0, 10, "", prompt_id="consumer", attachments=[link],
            document=document)],
        window_start=0, window_end=10, fps=24, template="standard")
    diagnostic = next(value for value in compiled["errors"]
                      if value["code"] == "broken_prompt_link")
    assert diagnostic["attachment_id"] == link["attachment_id"]


def test_cached_empty_prompt_link_warns_every_consumer_chip():
    source = _section(0, 10, "", prompt_id="source")
    links = [_link_attachment("source"), _link_attachment("source")]
    consumers = []
    for index, link in enumerate(links, start=1):
        consumers.append(_section(
            index * 10, (index + 1) * 10, "", prompt_id=f"consumer-{index}",
            attachments=[link], document={"nodes": [{
                "type": "attachment", "node_id": f"link-{index}",
                "attachment_id": link["attachment_id"],
            }]}))
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, *consumers],
        window_start=10, window_end=30, fps=24, template="standard")
    assert {value["attachment_id"] for value in compiled["warnings"]
            if value["code"] == "empty_prompt_link"} == {
                link["attachment_id"] for link in links}


def test_section_scope_prompt_link_emits_before_authored_text_and_deduplicates():
    source = _section(0, 10, "source text", prompt_id="source")
    link = _scope_link_attachment("source")
    consumer = _section(10, 20, "consumer text", prompt_id="consumer",
                        attachments=[link])
    selected = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, consumer], window_start=10,
        window_end=20, fps=24, template="standard")
    assert selected["prompt"] == "source text consumer text"
    combined = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, consumer], window_start=0,
        window_end=20, fps=24, template="standard")
    assert combined["prompt"] == "source text. consumer text"
    assert link["link_exportable"] is True


def test_scope_prompt_link_refuses_inline_anchor():
    link = _scope_link_attachment("source")
    consumer = _section(10, 20, "", prompt_id="consumer",
                        attachments=[link], document={"nodes": [{
                            "type": "attachment", "node_id": "bad-anchor",
                            "attachment_id": link["attachment_id"],
                        }]})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(
            0, 10, "source", prompt_id="source"), consumer],
        window_start=10, window_end=20, fps=24, template="standard")
    diagnostic = next(value for value in compiled["errors"]
                      if value["code"] == "anchored_scope_only_attachment")
    assert diagnostic["attachment_id"] == link["attachment_id"]


def test_scope_and_inline_prompt_links_choose_scope_phase_once():
    source = _section(0, 10, "source text", prompt_id="source")
    scope_link = _scope_link_attachment("source")
    inline_link = _link_attachment("source")
    consumer = _section(10, 20, "", prompt_id="consumer",
                        attachments=[scope_link, inline_link], document={"nodes": [
                            {"type": "text", "node_id": "before", "text": "before "},
                            {"type": "attachment", "node_id": "inline",
                             "attachment_id": inline_link["attachment_id"]},
                            {"type": "text", "node_id": "after", "text": " after"},
                        ]})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, consumer], window_start=10,
        window_end=20, fps=24, template="standard")
    assert compiled["prompt"].count("source text") == 1
    assert compiled["prompt"].startswith("source text before")


def test_section_scope_prompt_links_resolve_transitively():
    first = _section(0, 10, "first", prompt_id="first")
    middle = _section(10, 20, "middle", prompt_id="middle",
                      attachments=[_scope_link_attachment("first")])
    consumer = _section(20, 30, "consumer", prompt_id="consumer",
                        attachments=[_scope_link_attachment("middle")])
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[first, middle, consumer],
        window_start=20, window_end=30, fps=24, template="standard")
    assert compiled["prompt"] == "first middle consumer"


def test_managed_vocal_event_conflicts_with_literal_speaker_id():
    event = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"voice_id": "narrator"},
        "config": {"event_type": "dialogue", "text": "Hello"},
    })
    section = _section(0, 10, "(S1) waits", attachments=[event])
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[section], window_start=0,
        window_end=10, fps=24, template="standard")
    assert any(value["code"] == "managed_manual_speaker_conflict"
               for value in compiled["errors"])


def test_custom_profile_rejects_executable_formatter():
    with pytest.raises(ValueError, match="executable_formatter_forbidden"):
        prompt_context.normalize_profile({
            "profile_id": "bad", "version": "1", "template_id": "standard",
            "capabilities": {},
            "writing_aids": [{"id": "x", "text": "<script>alert(1)</script>"}],
        })


def test_custom_profile_validators_emit_configured_error_and_warning():
    profile = {
        "profile_id": "bounded", "version": "1", "template_id": "standard",
        "capabilities": {}, "writing_aids": [],
        "validators": [
            {"kind": "required_channel", "channel_key": "speech"},
            {"kind": "max_final_chars", "limit": 3, "severity": "warning"},
        ],
    }
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "long")],
        window_start=0, window_end=10, fps=24, template="standard",
        profile=profile)
    assert any(value["code"] == "profile_required_channel"
               for value in compiled["errors"])
    assert any(value["code"] == "profile_max_final_chars"
               for value in compiled["warnings"])


def test_custom_profile_versions_are_immutable_and_used_versions_cannot_be_removed():
    profile = prompt_context.normalize_profile({
        "profile_id": "custom", "version": "1", "template_id": "standard",
        "capabilities": {}, "writing_aids": [],
    })
    scene = Scene(scene_id="scene", prompt_context_profile_id="custom@1")
    project = TimelineProject(project_id="project", scenes=[scene],
                              prompt_context_profiles=[profile])
    changed = {**profile, "name": "Changed in place"}

    with pytest.raises(routes.ProjectMutationRequestError) as immutable:
        routes._normalize_prompt_context_profile_update(project, [changed])
    assert immutable.value.code == "immutable_prompt_context_profile"

    with pytest.raises(routes.ProjectMutationRequestError) as used:
        routes._normalize_prompt_context_profile_update(project, [])
    assert used.value.code == "prompt_context_profile_in_use"
    assert used.value.usages[0]["type"] == "scene"

    forked = prompt_context.normalize_profile({
        **profile, "version": "2", "name": "Forked version",
    })
    accepted = routes._normalize_prompt_context_profile_update(
        project, [profile, forked])
    assert [f"{value['profile_id']}@{value['version']}" for value in accepted] == [
        "custom@1", "custom@2"]


def test_reference_roles_follow_the_exact_active_custom_profile():
    profiles = [
        {"profile_id": "format_a", "version": "1",
         "role_catalogs": {"pictures": ["role_a"]}},
        {"profile_id": "format_b", "version": "1",
         "role_catalogs": {"pictures": ["role_b"]}},
    ]
    recipe = {"soft": {
        "compatible_profiles": ["format_a@1", "format_b@1"],
        "physical_population": "pictures",
    }}
    assert prompt_context.normalize_reference_role(
        "role_b", recipe, profiles=profiles,
        active_profile="format_b@1") == "role_b"
    with pytest.raises(ValueError, match="Unsupported Reference role"):
        prompt_context.normalize_reference_role(
            "role_a", recipe, profiles=profiles,
            active_profile="format_b@1")


def test_global_attachment_phases_are_deterministic():
    def attachment(identity, text, placement):
        return prompt_context.normalize_attachment({
            "attachment_id": identity, "kind": "custom", "config": {"text": text},
            "capabilities": [{"capability_id": identity, "kind": "custom",
                              "channel_key": "visual", "placement": placement}],
        })

    compiled = prompt_context.compile_prompt_context(
        global_channels={"visual": "body"},
        global_attachments=[
            attachment("suffix", "suffix", "channel_suffix"),
            attachment("preamble", "preamble", "document_preamble"),
            attachment("global", "global", "global_document"),
        ],
        sections=[], window_start=0, window_end=10, fps=24,
        template="standard")
    assert compiled["prompt"] == "preamble global body suffix"


@pytest.mark.parametrize("kind,code", [
    ("shot", "invalid_global_attachment"),
    ("timestamp", "invalid_global_attachment"),
    ("prompt_link", "invalid_global_attachment"),
    ("vocal_event", "global_vocal_event"),
])
def test_section_only_attachments_are_blocked_even_when_anchored_globally(kind, code):
    attachment = prompt_context.normalize_attachment({"kind": kind})
    compiled = prompt_context.compile_prompt_context(
        global_channels={"visual": "global"},
        global_documents={"visual": {"nodes": [
            {"type": "attachment", "node_id": "anchor",
             "attachment_id": attachment["attachment_id"]},
        ]}},
        global_attachments=[attachment], sections=[], window_start=0,
        window_end=10, fps=24, template="standard")
    diagnostic = next(value for value in compiled["errors"]
                      if value["code"] == code)
    assert diagnostic["attachment_id"] == attachment["attachment_id"]


def test_invalid_attachment_route_diagnostic_targets_the_chip():
    attachment = prompt_context.normalize_attachment({
        "kind": "custom", "config": {"text": "detail"},
        "capabilities": [{"capability_id": "bad-route", "kind": "custom",
                          "channel_key": "missing", "placement": "section_prefix"}],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(
            0, 10, "body", attachments=[attachment])],
        window_start=0, window_end=10, fps=24, template="standard")
    diagnostic = next(value for value in compiled["errors"]
                      if value["code"] == "invalid_attachment_route")
    assert diagnostic["attachment_id"] == attachment["attachment_id"]


def test_h3_custom_picture_guidance_text_must_bind_to_active_setup_role():
    guide = prompt_context.normalize_attachment({
        "kind": "custom", "config": {"text": "Use <Picture 1> as the pose"}})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "move", attachments=[guide])],
        window_start=0, window_end=10, fps=24, template="minimax_h3_base",
        context={"setup_manifest": {"setup": {"mode": "base", "task_mode": "T2VA"},
                                    "guides": []}})
    assert any(value["code"] == "unbound_h3_picture_guidance"
               for value in compiled["errors"])


def test_audio_definition_reuses_but_never_creates_speaker_identity():
    reference = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["granny"]},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "channel_key": "subject_definitions", "placement": "section_prefix",
                          "config": {"definition": "Granny", "audio_definition": "warm voice",
                                     "audio_speaker_subject_id": "granny"}}],
    })
    base_context = {
        "semantic_units": [{"semantic_unit_id": "granny", "name": "Granny",
                            "sources": [{"member_id": "voice"}]}],
        "ordinal_manifest": {"subjects": {"granny": 1}},
        "unit_source_labels": {"granny": ["<Audio 1>"]},
    }
    without_event = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "wait", attachments=[reference])],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        context={**base_context, "setup_manifest": {
            "setup": {"mode": "reference"}}})
    assert any(value["code"] == "unresolved_audio_speaker_binding"
               for value in without_event["errors"])

    vocal = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"subject_ids": ["granny"]},
        "config": {"event_type": "dialogue", "text": "Hello"}})
    with_event = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[_section(0, 10, "wait", attachments=[reference, vocal])],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        context={**base_context, "setup_manifest": {
            "setup": {"mode": "reference"}}})
    assert not any(value["code"] == "unresolved_audio_speaker_binding"
                   for value in with_event["errors"])
    assert "<Audio 1> is warm voice for <Subject 1> (S1)" in with_event["prompt"]


def test_managed_speaker_catalog_uses_effective_held_prompt_segments():
    vocal = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"subject_ids": ["held-subject"]},
        "config": {"event_type": "dialogue", "text": "Still speaking"},
    })
    section = _section(
        0, 10, "", prompt_id="held", attachments=[vocal], document={"nodes": [{
            "type": "attachment", "node_id": "vocal",
            "attachment_id": vocal["attachment_id"],
        }]})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[section], window_start=20, window_end=30,
        fps=24, template="standard")
    assert compiled["managed_speaker_subject_ids"] == ["held-subject"]


def test_managed_speaker_catalog_excludes_fully_shadowed_prompt_sections():
    vocal = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"subject_ids": ["shadowed-subject"]},
        "config": {"event_type": "dialogue", "text": "Hidden"},
    })
    winner = _section(0, 20, "winner", prompt_id="winner")
    shadowed = _section(
        5, 10, "", prompt_id="shadowed", attachments=[vocal], document={"nodes": [{
            "type": "attachment", "node_id": "vocal",
            "attachment_id": vocal["attachment_id"],
        }]})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[winner, shadowed],
        window_start=0, window_end=20, fps=24, template="standard")
    assert compiled["managed_speaker_subject_ids"] == []


def test_generic_reference_recipe_profile_and_capability_are_enforced():
    attachment = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"reference_item_id": "item"},
        "capabilities": [{"capability_id": "derived", "kind": "derived_prompt",
                          "channel_key": "visual"}],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "move", attachments=[attachment])],
        window_start=0, window_end=10, fps=24, template="standard",
        context={"generic_references": {"item": {
            "prompt": "reference", "compatible_profiles": ["minimax_h3_ref@1"],
            "exposed_capabilities": ["mentions"],
        }}})
    codes = {value["code"] for value in compiled["errors"]}
    assert {"reference_profile_incompatible", "reference_capability_incompatible"} <= codes


def test_h3_base_setup_requires_declared_guides():
    result = minimax_h3.resolve_setup(
        setup={"mode": "base", "task_mode": "FL2VA",
               "first_guide_id": "first", "last_guide_id": "last"},
        guide_frames=[GuideFrame(guide_id="first", asset_id="image")],
        scene_duration=100, window_start=0, window_end=100)
    assert [value["code"] for value in result["errors"]] == ["missing_last_guide"]


def test_h3_setup_overflow_is_preserved_until_validation():
    setup = minimax_h3.normalize_setup({
        "mode": "reference",
        "picture_lane_ids": [f"lane-{index}" for index in range(10)],
    })
    assert len(setup["picture_lane_ids"]) == 10
    result = minimax_h3.resolve_setup(
        setup=setup, reference_items=[], lane_recipes=[], lane_count=10,
        scene_duration=100, window_start=0, window_end=100)
    assert any(value["code"] == "missing_setup_lane"
               for value in result["errors"])


def test_h3_reference_presentation_order_and_independent_audio_ordinals():
    picture_asset = Asset(asset_id="pi", asset_type="image")
    video_asset = Asset(asset_id="vi", asset_type="video", duration_sec=5,
                        has_audio=True)
    audio_asset = Asset(asset_id="au", asset_type="audio", duration_sec=5)
    members = [
        ReferenceMember(member_id="pm", asset_id="pi",
                        prompt="an elderly Korean woman in a blue coat"),
        ReferenceMember(member_id="vm", asset_id="vi", name="Profile"),
        ReferenceMember(member_id="am", asset_id="au"),
    ]
    references = [ReferenceEntity(reference_id="entity", name="Granny", members=members)]
    recipes = [
        ReferenceLaneRecipe(lane_id="pictures", media_kind="image", recipe={"soft": {
            "compatible_profiles": ["minimax_h3_ref@1"],
            "physical_population": "pictures"}}),
        ReferenceLaneRecipe(lane_id="videos", media_kind="image", recipe={"soft": {
            "compatible_profiles": ["minimax_h3_ref@1"],
            "physical_population": "videos"}}),
        ReferenceLaneRecipe(lane_id="audio", media_kind="audio", recipe={"soft": {
            "compatible_profiles": ["minimax_h3_ref@1"],
            "physical_population": "standalone_audios"}}),
    ]
    items = [
        ReferenceItem(reference_item_id="p", lane_index=0, start_frame=0,
                      end_frame=100, members=[{"entity_id": "entity", "member_id": "pm"}]),
        ReferenceItem(reference_item_id="v", lane_index=1, start_frame=0,
                      end_frame=100, members=[{"entity_id": "entity", "member_id": "vm"}]),
        ReferenceItem(reference_item_id="a", lane_index=2, start_frame=0,
                      end_frame=100, members=[{"entity_id": "entity", "member_id": "am"}]),
    ]
    result = minimax_h3.resolve_setup(
        setup={"mode": "reference", "picture_lane_ids": ["pictures"],
               "video_lane_ids": ["videos"], "audio_lane_ids": ["audio"]},
        reference_items=items, lane_recipes=recipes, lane_count=3,
        scene_duration=100, window_start=0, window_end=100,
        references=references, assets=[picture_asset, video_asset, audio_asset],
        semantic_units=[{"semantic_unit_id": "video-subject", "order": 0,
                         "sources": [{"member_id": "vm"}]}])
    assert result["errors"] == []
    presentation = result["setup_manifest"]["presentation"]
    assert [value["kind"] for value in presentation] == [
        "picture", "video", "standalone_audio"]
    assert presentation[0]["member_prompt"] == (
        "an elderly Korean woman in a blue coat")
    assert presentation[1]["video_ordinal"] == 1
    assert presentation[1]["prompt_name"] == "Granny_Profile"
    assert presentation[1]["display_name"] == "Granny · Profile"
    assert presentation[2]["audio_ordinal"] == 1
    assert presentation[1]["decoded_frames_24fps"] == 107
    assert result["unit_source_labels"]["video-subject"] == ["<Video 1>"]


def test_retired_paired_audio_fields_are_tolerantly_dropped():
    setup = minimax_h3.normalize_setup({
        "mode": "reference",
        "paired_audio_bindings": [{"video_member_id": "old", "embedded": True}],
    })
    item = ReferenceItem.from_dict({
        "reference_item_id": "item",
        "members": [{"member_id": "old", "paired_audio_asset_id": "audio"}],
    })
    assert "paired_audio_bindings" not in setup
    assert "paired_audio_asset_id" not in item.members[0]


def test_project_round_trip_preserves_context_records_and_drops_subject_ids():
    section = _section(0, 10, "text", shot=True)
    serialized_section = section.to_dict()
    serialized_section["subject_ids"] = [
        {"entity_id": "old", "retention": "fully_preserved"}]
    scene = Scene(scene_id="scene", duration_frames=10)
    scene.prompt_sections = [PromptSection.from_dict(serialized_section)]
    scene.prompt_context_profile_id = "generic@1"
    project = TimelineProject(project_id="project", scenes=[scene])
    project.prompt_semantic_units = [prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "subject", "name": "Granny"})]
    restored = TimelineProject.from_dict(copy.deepcopy(project.to_dict()))
    restored_section = restored.scenes[0].prompt_sections[0]
    assert "subject_ids" not in restored_section.to_dict()
    assert restored_section.attachments[0]["kind"] == "shot"
    assert restored.prompt_semantic_units[0]["semantic_unit_id"] == "subject"


def test_server_prompt_history_freezes_custom_profile_and_subject_dependency_closure():
    profile = prompt_context.normalize_profile({
        "profile_id": "custom", "version": "1", "template_id": "standard",
        "capabilities": {}, "writing_aids": [],
    })
    unit = prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "subject", "name": "Subject",
        "sources": [{"entity_id": "entity", "member_id": "member"}],
    })
    attachment = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["subject"]},
    })
    project = TimelineProject(
        project_id="project", prompt_context_profiles=[profile],
        prompt_semantic_units=[unit])
    job = GenerationJob(
        scene_id="scene", selection_start=0, selection_end=10,
            scene_prompt="global", params={
                "snapshot_version": 1,
                "prompt_context_format": "prompt_context_v1",
            },
        prompt_sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 10,
            "channels": {"visual": "scene"}, "attachments": [attachment],
        }],
        compiled_prompt_context={"profile": profile},
        reference_input_snapshots=[{"kind": "semantic_unit", "value": unit}],
    )

    routes._record_prompt_history(project, [job])
    entry = project.metadata["prompt_history"][0]
    project.prompt_context_profiles.clear()
    project.prompt_semantic_units.clear()

    assert entry["prompt_context_profile_id"] == "custom@1"
    assert entry["prompt_context_profiles"] == [profile]
    assert entry["prompt_semantic_units"] == [unit]


def test_prompt_panel_escape_restores_structured_state_without_flattening_chips():
    root = Path(__file__).resolve().parents[1]
    editor = (root / "web" / "js" / "prompt_context_chips.js").read_text(
        encoding="utf-8")
    panel = (root / "web" / "js" / "editor_prompt_panel.js").read_text(
        encoding="utf-8")
    assert "promptState:" in editor
    assert "attachments: structuredClone(attachments)" in editor
    assert "if (focused.structured) focused.el.promptState = focused.revert" in panel
    assert "focused.el.value = focused.revert" in panel


def test_prompt_editors_claim_keyboard_and_compact_channels_without_flattening():
    root = Path(__file__).resolve().parents[1]
    chips = (root / "web" / "js" / "prompt_context_chips.js").read_text(
        encoding="utf-8")
    keyboard = (root / "web" / "js" / "keyboard_ownership.js").read_text(
        encoding="utf-8")
    widget = (root / "web" / "js" / "editor_widget.js").read_text(
        encoding="utf-8")
    extension = (root / "web" / "js" / "extension.js").read_text(
        encoding="utf-8")
    assert "PRESERVE_DEFAULT" in keyboard and "TEXT_EDITOR: 75" in keyboard
    assert 'id: "sonder-prompt-text-editor"' in chips
    assert "isPromptTextEditorFocused" in extension
    assert 'reason: promptTextEditing ? "prompt-text-editor"' in extension
    assert "selection: selectionBookmark()" in chips
    assert "render(previous.selection)" in chips
    assert "render(next.selection)" in chips
    assert "syncPromptAttachments" in chips
    assert "addOwnedKeyHandler" in chips
    assert "Remove ${label} context chip" in chips
    assert "sonder.prompt.channelView.v2." in widget
    assert '? remembered : "__all__"' in widget
    assert "Finish composing text before switching prompt channels." in widget
    assert "hidden with text" in widget
    assert "isContentEditable" in widget
    theme_import = widget.split('from "./editor_theme.js";', 1)[0].rsplit(
        "import {", 1)[-1]
    assert "chromeSelectCss" in theme_import


def test_writing_split_can_retain_stable_empty_blocks_and_link_defaults():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for Prompt Context JS tests")
    module_url = (Path(__file__).resolve().parents[1] / "web" / "js" /
                  "prompt_context_chips.js").as_uri()
    script = f"""
        const mod = await import({json.dumps(module_url)});
        const documentValue = {{nodes: [
            {{type:'text', node_id:'left', text:''}},
            {{type:'text', node_id:'separator', text:'\\n---\\n'}},
            {{type:'text', node_id:'right', text:''}},
        ]}};
        console.log(JSON.stringify({{
            kept: mod.splitWritingPromptDocument(documentValue, {{keepEmpty:true}}).length,
            dropped: mod.splitWritingPromptDocument(documentValue).length,
            linkExportable: mod.normalizePromptAttachment({{kind:'prompt_link'}}).link_exportable,
        }}));
    """
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], capture_output=True,
        text=True, encoding="utf-8", check=True)
    assert json.loads(result.stdout) == {
        "kept": 2, "dropped": 0, "linkExportable": True}


def test_prompt_editor_sources_preserve_writing_state_and_prune_deleted_chips():
    root = Path(__file__).resolve().parents[1]
    editor = (root / "web" / "js" / "prompt_context_chips.js").read_text(
        encoding="utf-8")
    panel = (root / "web" / "js" / "editor_prompt_panel.js").read_text(
        encoding="utf-8")
    identity_panel = (root / "web" / "js" / "prompt_identity_panel.js").read_text(
        encoding="utf-8")
    widget = (root / "web" / "js" / "editor_widget.js").read_text(
        encoding="utf-8")
    assert "removedInline" in editor
    assert "range.intersectsNode(value)" in editor
    assert "if (!composing) pushHistory()" in editor
    assert "global_channel_exceptions" in panel
    assert "muted: writingState.blockMeta" in panel
    assert "physicalOptions" in editor
    assert "candidate?.setup_manifest" in identity_panel
    assert "Staging remains in Reference lanes" in identity_panel
    assert 'type: "ensure_minimax_h3_reference_population"' not in panel
    assert "pre_context_frames: this._contextFrameValue" in widget
    assert "frame_constraint: this._getActiveFrameConstraint()" in widget


def test_prompt_frontend_preserves_profile_aids_dependency_history_and_split_conflicts():
    root = Path(__file__).resolve().parents[1]
    panel = (root / "web" / "js" / "editor_prompt_panel.js").read_text(
        encoding="utf-8")
    widget = (root / "web" / "js" / "editor_widget.js").read_text(
        encoding="utf-8")
    chips = (root / "web" / "js" / "prompt_context_chips.js").read_text(
        encoding="utf-8")
    assert "descriptor?.fork_seed" in panel
    assert "const writingAids = structuredClone(selected.writing_aids || [])" in panel
    assert 'makeBtn("Add writing aid"' in panel and "writingAids.splice(index, 1)" in panel
    assert "prompt-profile-immutable" in panel
    assert "delete semantic.attachment_id" in panel
    assert "_pushProjectDependencyUndo" in widget
    assert 'kind: "project_dependencies"' in widget
    assert "_restoreProjectDependencies" in widget
    assert "Prompt parts and routing" in chips
    assert "capabilityRows" in chips


def test_out_of_window_broken_reference_does_not_block_selected_section():
    broken = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"reference_item_id": "missing"},
        "capabilities": [{"capability_id": "derived_prompt",
                          "kind": "derived_prompt", "channel_key": "visual",
                          "placement": "section_prefix"}],
    })
    dormant = _section(0, 10, "dormant", prompt_id="dormant",
                       attachments=[broken])
    selected = _section(10, 20, "selected", prompt_id="selected")
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[dormant, selected],
        window_start=10, window_end=20, fps=24, template="standard")
    assert compiled["prompt"] == "selected"
    assert not any(value["code"] == "reference_source_not_applicable"
                   for value in compiled["errors"])


def test_out_of_window_prompt_link_consumer_cannot_claim_selected_fallback():
    source = _section(0, 10, "SOURCE", prompt_id="source")
    out_link = _link_attachment("source")
    selected_link = _link_attachment("source")
    out_document = {"nodes": [
        {"type": "attachment", "node_id": "out-link",
         "attachment_id": out_link["attachment_id"]},
        {"type": "text", "node_id": "out-text", "text": " OUT"},
    ]}
    selected_document = {"nodes": [
        {"type": "attachment", "node_id": "selected-link",
         "attachment_id": selected_link["attachment_id"]},
        {"type": "text", "node_id": "selected-text", "text": " SELECTED"},
    ]}
    out_consumer = _section(10, 20, "", prompt_id="out-consumer",
                            attachments=[out_link], document=out_document)
    selected_consumer = _section(
        20, 30, "", prompt_id="selected-consumer",
        attachments=[selected_link], document=selected_document)
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, out_consumer, selected_consumer],
        window_start=20, window_end=30, fps=24, template="standard")
    assert compiled["prompt"] == "SOURCE SELECTED"


def test_prompt_links_export_transitive_dependency_edges_by_default():
    source = _section(0, 10, "A", prompt_id="source")
    middle_link = _link_attachment("source")
    final_link = _link_attachment("middle")
    middle = _section(10, 20, "", prompt_id="middle",
                      attachments=[middle_link], document={"nodes": [
                          {"type": "attachment", "node_id": "middle-link",
                           "attachment_id": middle_link["attachment_id"]},
                          {"type": "text", "node_id": "middle-text", "text": " B"},
                      ]})
    final = _section(20, 30, "", prompt_id="final",
                     attachments=[final_link], document={"nodes": [
                         {"type": "attachment", "node_id": "final-link",
                          "attachment_id": final_link["attachment_id"]},
                         {"type": "text", "node_id": "final-text", "text": " C"},
                     ]})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, middle, final],
        window_start=20, window_end=30, fps=24, template="standard")
    assert compiled["prompt"] == "A B C"


def test_prompt_link_does_not_duplicate_selected_attachment_only_source():
    guide = prompt_context.normalize_attachment({
        "kind": "custom", "config": {"text": "GUIDE"},
        "link_exportable": True,
    })
    source = _section(0, 10, "", prompt_id="source", attachments=[guide],
                      document={"nodes": [{
                          "type": "attachment", "node_id": "guide-node",
                          "attachment_id": guide["attachment_id"],
                      }]})
    link = _link_attachment("source")
    consumer = _section(10, 20, "", prompt_id="consumer",
                        attachments=[link], document={"nodes": [
                            {"type": "attachment", "node_id": "link-node",
                             "attachment_id": link["attachment_id"]},
                            {"type": "text", "node_id": "tail", "text": " NEXT"},
                        ]})

    combined = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, consumer], window_start=0,
        window_end=20, fps=24, template="standard")
    consumer_only = prompt_context.compile_prompt_context(
        global_channels={}, sections=[source, consumer], window_start=10,
        window_end=20, fps=24, template="standard")

    assert combined["prompt"].count("GUIDE") == 1
    assert consumer_only["prompt"] == "GUIDE NEXT"


def test_h3_physical_picture_definition_is_late_bound():
    reference = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"picture_ids": ["picture-member"]},
        "config": {"definition": "a red wooden door",
                   "retention_detail": "keep its painted panels"},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "channel_key": "subject_definitions",
                          "placement": "section_prefix"}],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(
            0, 10, "move", attachments=[reference])],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        context={
            "ordinal_manifest": {"pictures": {"picture-member": 1}},
            "setup_manifest": {"setup": {"mode": "reference"},
                               "pictures": [{"member_id": "picture-member",
                                             "picture_ordinal": 1}]},
        })
    assert "<Picture 1> is a red wooden door" in compiled["prompt"]
    assert not any(value["code"] == "missing_h3_physical_definition"
                   for value in compiled["errors"] + compiled["warnings"])


def test_minimax_managed_group_and_voiceover_syntax_matches_guide():
    group = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"subject_ids": ["one", "two"]},
        "config": {"event_type": "group_speech", "language": "English",
                   "subject_phrase": "The two children", "text": "Wait!"},
    })
    voiceover = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"subject_ids": ["one"]},
        "config": {"event_type": "voiceover", "language": "English",
                   "subject_phrase": "The older child", "text": "I remember."},
    })
    # Vocal Events are inline-only: their place in the document is their place
    # in the spoken order, so the section anchors both chips.
    section = PromptSection(
        start_frame=0, end_frame=10, attachments=[group, voiceover],
        channel_docs={"integrated_multimodal_description": {"nodes": [
            {"type": "text", "text": "scene. "},
            {"type": "attachment",
             "attachment_id": group["attachment_id"]},
            {"type": "text", "text": " "},
            {"type": "attachment",
             "attachment_id": voiceover["attachment_id"]},
        ]}},
    )
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[section],
        window_start=0, window_end=10, fps=24, template="minimax_h3_base",
            context={"setup_manifest": {"setup": {"mode": "base",
                                                    "task_mode": "T2VA"}},
                     "semantic_units": [
                         {"semantic_unit_id": "one", "name": "Older child",
                          "kind": "subject", "definition": "the older child"},
                         {"semantic_unit_id": "two", "name": "Younger child",
                          "kind": "subject", "definition": "the younger child"},
                     ]})
    assert "The two children (S1,S2) say together:" in compiled["prompt"]
    assert "says in an off-screen voiceover:" in compiled["prompt"]
    assert "lips remain completely closed." in compiled["prompt"]
    assert not compiled["errors"]


def test_scope_vocal_event_and_prompt_link_block_instead_of_silently_dropping():
    event = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"subject_ids": ["one"]},
        "config": {"event_type": "dialogue", "language": "English",
                   "text": "Hello."},
    })
    link = prompt_context.normalize_attachment({
        "kind": "prompt_link", "source": {"prompt_id": "first",
                                          "channel_key": "visual"},
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[_section(0, 10, "first", prompt_id="first"),
                  _section(10, 20, "second", prompt_id="second",
                           attachments=[event, link])],
        window_start=0, window_end=20, fps=24, template="standard")
    codes = [value["code"] for value in compiled["errors"]]
    assert codes.count("unanchored_inline_attachment") == 2
    assert {value["attachment_id"] for value in compiled["errors"]
            if value["code"] == "unanchored_inline_attachment"} == {
                event["attachment_id"], link["attachment_id"]}


def test_h3_task_types_require_explicit_audio_role_not_preservation_default():
    inherited_only = {"setup_manifest": {"standalone_audios": [{
        "member_id": "audio", "role": "",
        "audio_intent": "reference_characteristics",
    }]}}
    explicit = {"setup_manifest": {"standalone_audios": [{
        "member_id": "audio", "role": "audio_reference",
        "audio_intent": "reference_characteristics",
    }]}}
    profile = prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"]
    assert prompt_context._minimax_task_types(
        inherited_only, profile=profile) == []
    assert prompt_context._minimax_task_types(
        explicit, profile=profile) == ["audio reference"]


def test_h3_summary_task_type_selection_overrides_role_derived_defaults():
    context = {"setup_manifest": {
        "pictures": [{"member_id": "picture", "role": "identity"}],
        "videos": [{"member_id": "video", "role": "video_editing"}],
    }}
    profile = prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"]
    assert prompt_context._minimax_task_types(context, profile=profile) == [
        "reference generation", "video editing"]
    assert prompt_context._minimax_task_types(
        context, ["video continuation", "keyframe completion"], profile) == [
            "keyframe completion", "video continuation"]


def test_h3_subject_definition_inherits_contributing_library_member_prompt():
    reference = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["subject"]},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "channel_key": "subject_definitions",
                          "placement": "section_prefix"}],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(
            0, 10, "move", attachments=[reference])],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        context={
            "setup_manifest": {
                "setup": {"mode": "reference"},
                "pictures": [{"member_id": "portrait", "picture_ordinal": 1}],
                "presentation": [{
                    "kind": "picture", "member_id": "portrait",
                    "member_prompt": "a poised Korean woman in a blue coat",
                    "picture_ordinal": 1,
                }],
            },
            "ordinal_manifest": {
                "subjects": {"subject": 1}, "pictures": {"portrait": 1}},
            "unit_source_labels": {"subject": ["<Picture 1>"]},
            "semantic_units": [{
                "semantic_unit_id": "subject", "name": "Korean Woman",
                "definition": "", "sources": [{
                    "entity_id": "woman", "member_id": "portrait"}],
            }],
        })
    assert compiled["channels"]["subject_definitions"] == (
        "<Subject 1> is a poised Korean woman in a blue coat from <Picture 1>")
    assert not any(value["code"] == "missing_h3_subject_definition"
                   for value in compiled["errors"] + compiled["warnings"])
    assert compiled["attachment_previews"][reference["attachment_id"]] == (
        "<Subject 1> is a poised Korean woman in a blue coat from <Picture 1>")


def test_h3_subject_definition_precedence_is_chip_then_subject_then_member():
    context = {"setup_manifest": {"presentation": [{
        "member_id": "portrait", "member_prompt": "member prose"}]}}
    unit = {"definition": "subject prose",
            "sources": [{"member_id": "portrait"}]}
    assert prompt_context._subject_definition(
        {"definition": "chip prose"}, unit, context) == ("chip prose", "chip")
    assert prompt_context._subject_definition({}, unit, context) == (
        "subject prose", "subject")
    assert prompt_context._subject_definition(
        {}, {**unit, "definition": ""}, context) == ("member prose", "member")


def test_blank_reference_override_does_not_hide_conflicting_staged_intents():
    attachment = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["subject"]},
        "config": {"visual_intent": ""},
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(
            0, 10, "scene", attachments=[attachment])],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        context={
            "setup_manifest": {
                "setup": {"mode": "reference"},
                "pictures": [
                    {"member_id": "one", "visual_intent": "preserve"},
                    {"member_id": "two", "visual_intent": "partial"},
                ],
            },
            "ordinal_manifest": {"subjects": {"subject": 1}},
            "semantic_units": [{
                "semantic_unit_id": "subject", "name": "Subject",
                "definition": "a person", "sources": [
                    {"entity_id": "entity", "member_id": "one"},
                    {"entity_id": "entity", "member_id": "two"}],
            }],
        })
    assert any(value["code"] == "conflicting_reference_intent"
               for value in compiled["errors"])


def test_custom_capability_formatter_is_bounded_and_text_alias_resolves():
    with pytest.raises(ValueError, match="formatter_too_large"):
        prompt_context.normalize_profile({
            "profile_id": "large", "version": "1", "template_id": "standard",
            "capabilities": {"custom": {"formatter": "x" * 4097}},
            "writing_aids": [],
        })
    custom = prompt_context.normalize_attachment({
        "kind": "custom", "config": {"text": "hello"}})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(
            0, 10, "", attachments=[custom])],
        window_start=0, window_end=10, fps=24, template="standard",
        profile={"profile_id": "custom", "version": "1",
                 "template_id": "standard", "writing_aids": [],
                 "capabilities": {"custom": {"channel_key": "visual",
                                                 "placement": "section_prefix",
                                                 "formatter": "prefix {text}"}}})
    assert compiled["prompt"] == "prefix hello"
