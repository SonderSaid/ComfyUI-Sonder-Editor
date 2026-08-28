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
    assert not [row for row in compiled["emissions"]
                if row["attachment_id"] == attachment["attachment_id"]]


@pytest.mark.parametrize("kind", [
    "shot", "timestamp", "prompt_link", "prompt_link_scope", "vocal_event",
])
def test_disabled_global_section_only_attachment_is_inert(kind):
    attachment = prompt_context.normalize_attachment({
        "kind": kind, "enabled": False,
    })
    compiled = prompt_context.compile_prompt_context(
        global_attachments=[attachment], sections=[], window_start=0,
        window_end=10, fps=24, template="standard")
    assert not [row for row in compiled["errors"]
                if row.get("attachment_id") == attachment["attachment_id"]]
    assert not [row for row in compiled["warnings"]
                if row.get("attachment_id") == attachment["attachment_id"]]
    assert not [row for row in compiled["emissions"]
                if row["attachment_id"] == attachment["attachment_id"]]


def test_invalid_global_vocal_event_cannot_preempt_a_valid_section_clone():
    profile = {
        "profile_id": "voice_owner", "version": "1", "name": "Voice owner",
        "template_id": "standard", "writing_aids": [],
        "capabilities": {"vocal_event": {
            "channel_key": "visual", "placement": "inline",
        }},
        "identity_kinds": [prompt_context.MINIMAX_SUBJECT_KIND],
    }

    def vocal_event(attachment_id, text):
        return prompt_context.normalize_attachment({
            "attachment_id": attachment_id,
            "emission_group_id": "shared-vocal-event",
            "kind": "vocal_event",
            "source": {"subject_ids": ["narrator"]},
            "config": {
                "event_type": "narration", "language": "English",
                "subject_phrase": "the narrator", "text": text,
            },
        })

    global_vocal = vocal_event("global-vocal", "GLOBAL LINE")
    section_vocal = vocal_event("section-vocal", "SECTION LINE")
    section_document = {"nodes": [{
        "type": "attachment", "node_id": "voice-anchor",
        "attachment_id": "section-vocal", "capability_id": "vocal_event",
    }]}
    compiled = prompt_context.compile_prompt_context(
        global_attachments=[global_vocal],
        sections=[PromptSection(
            0, 10, attachments=[section_vocal],
            channel_docs={"visual": section_document})],
        window_start=0, window_end=10, fps=24, template="standard",
        profile=profile, context={"semantic_units": [{
            "semantic_unit_id": "narrator", "kind": "subject",
            "name": "Narrator", "definition": "the narrator", "sources": [],
        }]})

    assert "SECTION LINE" in compiled["prompt"]
    assert "GLOBAL LINE" not in compiled["prompt"]
    assert [row["attachment_id"] for row in compiled["emissions"]] == [
        "section-vocal"]
    assert [row["attachment_id"] for row in compiled["errors"]
            if row["code"] == "global_vocal_event"] == ["global-vocal"]
    assert not [row for row in compiled["errors"]
                if row["code"] == "conflicting_emission"]


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


def test_audio_definition_derives_speaker_from_its_owning_identity():
    reference = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["granny"]},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "channel_key": "subject_definitions", "placement": "section_prefix",
                          "config": {"definition": "Granny",
                                     "audio_definition": "warm voice"}}],
    })
    base_context = {
        "semantic_units": [{"semantic_unit_id": "granny", "name": "Granny",
                            "sources": [{"entity_id": "e",
                                         "member_id": "voice"}]}],
        "ordinal_manifest": {"subjects": {"granny": 1}},
        "unit_source_labels": {"granny": ["<Audio 1>"]},
        "unit_source_members": {"granny": ["voice"]},
    }
    without_event = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "wait", attachments=[reference])],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        profile="minimax_h3_ref@1",
        context={**base_context, "setup_manifest": {
            "setup": {"mode": "reference"}}})
    assert "<Audio 1> is warm voice" in without_event["prompt"]
    assert "<Audio 1> is warm voice for" not in without_event["prompt"]
    assert without_event["errors"] == []
    assert not any(value["code"] == "stable_voice_event_not_identity_speaker"
                   for value in without_event["warnings"])

    vocal = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"subject_ids": ["granny"]},
        "config": {"event_type": "dialogue", "text": "Hello"}})
    with_event = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[PromptSection(
            0, 10, attachments=[reference, vocal],
            channel_docs={"detailed_description": {"nodes": [
                {"type": "text", "text": "wait "},
                {"type": "attachment", "node_id": "vocal",
                 "attachment_id": vocal["attachment_id"]},
            ]}})],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        profile="minimax_h3_ref@1",
        context={**base_context, "setup_manifest": {
            "setup": {"mode": "reference"}}})
    assert "<Audio 1> is warm voice for <Subject 1> (S1)" in with_event["prompt"]


def test_stale_audio_speaker_binding_is_advisory_and_ignored():
    reference = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["owner"]},
        "config": {"audio_speaker_subject_id": "other"},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "channel_key": "subject_definitions",
                          "placement": "section_prefix",
                          "config": {"definition": "Owner",
                                     "audio_definition": "owner voice"}}],
    })
    vocal = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"subject_ids": ["owner"]},
        "config": {"event_type": "dialogue", "text": "Hello"}})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[PromptSection(
            0, 10, attachments=[reference, vocal],
            channel_docs={"detailed_description": {"nodes": [
                {"type": "text", "text": "wait "},
                {"type": "attachment", "node_id": "vocal",
                 "attachment_id": vocal["attachment_id"]},
            ]}})],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        profile="minimax_h3_ref@1",
        context={
            "semantic_units": [
                {"semantic_unit_id": "owner", "name": "Owner",
                 "sources": [{"entity_id": "e", "member_id": "voice"}]},
                {"semantic_unit_id": "other", "name": "Other", "sources": []}],
            "ordinal_manifest": {"subjects": {"owner": 1, "other": 2}},
            "unit_source_labels": {"owner": ["<Audio 1>"]},
            "unit_source_members": {"owner": ["voice"]},
            "setup_manifest": {"setup": {"mode": "reference"}},
        })
    assert "<Audio 1> is owner voice for <Subject 1> (S1)" in compiled["prompt"]
    assert compiled["errors"] == []
    assert [value["code"] for value in compiled["warnings"]].count(
        "stale_audio_speaker_binding") == 1


def test_stale_audio_speaker_binding_on_standalone_audio_is_advisory():
    reference = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"audio_ids": ["voice"]},
        "config": {"audio_speaker_subject_id": "speaker"},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "channel_key": "subject_definitions",
                          "placement": "section_prefix",
                          "config": {"audio_definition": "warm voice"}}],
    })
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[PromptSection(
            0, 10, attachments=[reference],
            channels={"detailed_description": "scene"})],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        profile="minimax_h3_ref@1",
        context={
            "references": [{
                "reference_id": "entity", "name": "Voice", "members": [{
                    "member_id": "voice", "asset_id": "audio",
                    "prompt": "warm voice"}]}],
            "setup_manifest": {
                "setup": {"mode": "reference"},
                "standalone_audios": [{"member_id": "voice", "audio_ordinal": 1}],
                "presentation": [{"kind": "audio", "member_id": "voice",
                                  "member_prompt": "warm voice",
                                  "audio_ordinal": 1}],
            },
            "ordinal_manifest": {"audios": {"voice": 1}},
        })
    assert compiled["errors"] == []
    assert [value["code"] for value in compiled["warnings"]].count(
        "stale_audio_speaker_binding") == 1


def test_stable_voice_key_event_does_not_claim_an_identity_speaker():
    reference = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["owner"]},
        "capabilities": [{"capability_id": "definitions", "kind": "definitions",
                          "channel_key": "subject_definitions",
                          "placement": "section_prefix",
                          "config": {"definition": "Owner",
                                     "audio_definition": "owner voice"}}],
    })
    vocal = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"voice_id": "provider-key"},
        "config": {"event_type": "dialogue", "text": "Hello"}})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[PromptSection(
            0, 10, attachments=[reference, vocal],
            channel_docs={"detailed_description": {"nodes": [
                {"type": "text", "text": "wait "},
                {"type": "attachment", "node_id": "vocal",
                 "attachment_id": vocal["attachment_id"]},
            ]}})],
        window_start=0, window_end=10, fps=24, template="minimax_h3_ref",
        profile="minimax_h3_ref@1",
        context={
            "semantic_units": [{
                "semantic_unit_id": "owner", "name": "Owner",
                "sources": [{"entity_id": "e", "member_id": "voice"}]}],
            "ordinal_manifest": {"subjects": {"owner": 1}},
            "unit_source_labels": {"owner": ["<Audio 1>"]},
            "unit_source_members": {"owner": ["voice"]},
            "setup_manifest": {"setup": {"mode": "reference"}},
        })
    assert "<Audio 1> is owner voice for" not in compiled["prompt"]
    assert compiled["errors"] == []
    advisory = next(value for value in compiled["warnings"]
                    if value["code"] == "stable_voice_event_not_identity_speaker")
    assert advisory["attachment_id"] == vocal["attachment_id"]
    assert advisory["attachment_id"] != reference["attachment_id"]


def test_stable_voice_key_advisory_is_not_emitted_for_h3_base():
    vocal = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"voice_id": "provider-key"},
        "config": {"event_type": "dialogue", "text": "Hello"}})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[PromptSection(
            0, 10, attachments=[vocal],
            channel_docs={"detailed_description": {"nodes": [
                {"type": "attachment", "node_id": "vocal",
                 "attachment_id": vocal["attachment_id"]},
            ]}})],
        window_start=0, window_end=10, fps=24, template="minimax_h3_base",
        profile="minimax_h3_base@1",
        context={"setup_manifest": {}, "ordinal_manifest": {}},
    )
    assert not any(value["code"] == "stable_voice_event_not_identity_speaker"
                   for value in compiled["warnings"])


def test_identity_dependency_closure_ignores_stored_audio_speaker_residue():
    attachments = [{
        "kind": "reference", "source": {"semantic_unit_ids": ["owner"]},
        "config": {"audio_speaker_subject_id": "stale"},
    }]
    assert prompt_context.semantic_identity_dependency_ids(attachments) == ["owner"]


def test_legacy_voice_is_preserved_unread_and_inherit_description_is_retired():
    unit = prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "legacy", "name": "Legacy",
        "sources": [{
            "entity_id": "ref", "member_id": "member",
            "contribution": "appearance", "inherit_description": True,
        }],
        "voice": {"member_id": "member"},
    })
    assert unit["voice"] == {"member_id": "member"}
    assert "inherit_description" not in unit["sources"][0]
    compiled = prompt_context.compile_prompt_context(
        sections=[], window_start=0, window_end=10, fps=24,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={"semantic_units": [unit],
                 "setup_manifest": {"setup": {"mode": "reference"}}})
    warning = next(value for value in compiled["warnings"]
                   if value["code"] == "legacy_voice_binding")
    assert warning["semantic_unit_id"] == "legacy"


def test_speaking_identity_with_unstaged_durable_source_stays_applicable():
    vocal = prompt_context.normalize_attachment({
        "kind": "vocal_event", "source": {"subject_ids": ["other"]},
        "config": {"event_type": "dialogue", "text": "Still here"},
    })
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(
            0, 10, attachments=[vocal],
            channel_docs={"detailed_description": {"nodes": [{
                "type": "attachment", "node_id": "vocal",
                "attachment_id": vocal["attachment_id"],
            }]}})],
        window_start=0, window_end=10, fps=24,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "semantic_units": [{
                "semantic_unit_id": "other", "handle": "Other",
                "name": "Other", "definition": "the off-screen guide",
                "sources": [{"entity_id": "ref", "member_id": "unstaged"}],
            }],
            "unit_source_labels": {}, "unit_source_members": {},
            "ordinal_manifest": {},
            "setup_manifest": {"setup": {"mode": "reference"}},
        })
    assert compiled["errors"] == []
    assert "the off-screen guide (S1) says" in compiled["prompt"]


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
    """Normalization still preserves an over-cap declaration rather than trimming.

    Nothing reads these ids any more — Reference lane membership is derived
    from each recipe's declared model input — but a stored record is preserved
    as authored rather than rewritten at rest.
    """
    setup = minimax_h3.normalize_setup({
        "mode": "reference",
        "picture_lane_ids": [f"lane-{index}" for index in range(10)],
    })
    assert len(setup["picture_lane_ids"]) == 10
    result = minimax_h3.resolve_setup(
        setup=setup, reference_items=[], lane_recipes=[], lane_count=10,
        scene_duration=100, window_start=0, window_end=100)
    assert result["errors"] == []
    assert result["setup_manifest"]["pictures"] == []


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
    escape_start = panel.index("if (focused.structured)")
    escape_restore = panel[escape_start:panel.index("focused.el.blur()", escape_start)]
    assert "focused.el.promptState = focused.revert" in escape_restore
    assert "focused.el._sonderSyncDraft?.()" in escape_restore
    assert "focused.el.value = focused.revert" in escape_restore


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
    # The draft projection moved out of the Apply/preview closures into the
    # exported `writingSectionsFromDraft`, which reads its block metadata
    # positionally. Behaviour is covered properly by the projection tests;
    # this row only pins that `muted` still travels with the block.
    assert "muted: blockMeta[index]?.muted" in panel
    assert "physicalOptions" in editor
    assert "candidate?.setup_manifest" in identity_panel
    assert "Staging remains in Reference lanes" in identity_panel
    assert "ensure_minimax_h3_reference_population" not in panel
    # Reference mode reads no setup record, so nothing offers to author one.
    assert "minimax_h3_conditioning_setups" not in editor
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
            "unit_source_members": {"subject": ["portrait"]},
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


def test_h3_audio_member_prose_stays_out_of_the_visual_definition():
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
                "standalone_audios": [{"member_id": "voice", "audio_ordinal": 1}],
                "presentation": [
                    {"kind": "picture", "member_id": "portrait",
                     "member_prompt": "a poised Korean woman in a blue coat",
                     "picture_ordinal": 1},
                    {"kind": "audio", "member_id": "voice",
                     "member_prompt": "a soft voice with a slight Korean accent",
                     "audio_ordinal": 1},
                ],
            },
            "ordinal_manifest": {
                "subjects": {"subject": 1}, "pictures": {"portrait": 1},
                "audios": {"voice": 1}},
            "unit_source_labels": {
                "subject": ["<Picture 1>", "<Audio 1>"]},
            "unit_source_members": {"subject": ["portrait", "voice"]},
            "semantic_units": [{
                "semantic_unit_id": "subject", "name": "Korean Woman",
                "definition": "", "sources": [
                    {"entity_id": "woman", "member_id": "portrait"},
                    {"entity_id": "woman", "member_id": "voice"}],
            }],
        })
    assert compiled["channels"]["subject_definitions"].splitlines() == [
        "<Subject 1> is a poised Korean woman in a blue coat from <Picture 1>",
        "<Audio 1> is a soft voice with a slight Korean accent",
    ]
    assert not any(value["code"] == "missing_h3_audio_definition"
                   for value in compiled["errors"] + compiled["warnings"])


def test_h3_each_audio_member_needs_definition_prose():
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
                "standalone_audios": [
                    {"member_id": "voice-one", "audio_ordinal": 1},
                    {"member_id": "voice-two", "audio_ordinal": 2}],
                "presentation": [
                    {"kind": "audio", "member_id": "voice-one",
                     "member_prompt": "the first voice", "audio_ordinal": 1},
                    {"kind": "audio", "member_id": "voice-two",
                     "member_prompt": "", "audio_ordinal": 2}],
            },
            "ordinal_manifest": {
                "subjects": {"subject": 1},
                "audios": {"voice-one": 1, "voice-two": 2}},
            "unit_source_labels": {
                "subject": ["<Audio 1>", "<Audio 2>"]},
            "unit_source_members": {
                "subject": ["voice-one", "voice-two"]},
            "semantic_units": [{
                "semantic_unit_id": "subject", "name": "Speaker",
                "definition": "authored appearance", "sources": [
                    {"entity_id": "speaker", "member_id": "voice-one"},
                    {"entity_id": "speaker", "member_id": "voice-two"}],
            }],
        })
    assert "<Audio 1> is the first voice" in compiled["channels"][
        "subject_definitions"]
    assert "<Audio 2>" not in compiled["channels"]["subject_definitions"]
    assert any(value["code"] == "missing_h3_audio_definition"
               for value in compiled["warnings"])


def test_h3_subject_definition_precedence_is_chip_then_subject_then_member():
    context = {
        "profile": prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"],
        "setup_manifest": {"presentation": [{
            "member_id": "portrait", "member_prompt": "member prose"}]},
        "unit_source_labels": {"subject": ["<Picture 1>"]},
        "unit_source_members": {"subject": ["portrait"]},
    }
    unit = {"semantic_unit_id": "subject", "definition": "subject prose",
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


_MEMBER_TIER_PROFILE = {
    "name": "Tier Format",
    "physical_populations": [
        {"key": "pictures", "ordinal_key": "pictures",
         "source_key": "picture_ids", "token_kind": "picture",
         "label": "Picture", "label_template": "<Picture {n}>"},
    ],
    "capabilities": {"reference": {
        "defaults": {"retention_detail": "format detail"},
        "derived": {"retention": {"order": 1, "channel_key": "ret",
                                  "placement": "section_prefix", "fields": {}}},
    }},
}


def _member_tier_context(members, units=None):
    return {
        "profile": _MEMBER_TIER_PROFILE,
        "references": [{"reference_id": "ref", "name": "Korean Woman",
                        "members": members}],
        "semantic_units_by_id": {
            str(unit["semantic_unit_id"]): unit for unit in (units or [])},
    }


def test_member_attachment_defaults_sit_between_format_and_identity():
    """The physical member tier is what makes 'sparse deviations' true.

    Without it a physical Reference inherited only prompt text and the two
    intents, so every other field was a per-chip deviation.
    """
    members = [{"member_id": "pm", "handle": "CharacterSheet",
                "attachment_defaults": {"retention_detail": "member detail"}}]
    capability = {"capability_id": "retention", "kind": "retention"}

    # Direct physical selection: member beats the format default.
    physical = prompt_context.effective_reference_config(
        {"source": {"picture_ids": ["pm"]}, "config": {}},
        capability, _member_tier_context(members))
    assert physical["retention_detail"] == "member detail"

    # Via an identity that draws on the same member, with no identity default:
    # the member still supplies it.
    via_identity = prompt_context.effective_reference_config(
        {"source": {"semantic_unit_ids": ["lead"]}, "config": {}},
        capability, _member_tier_context(members, [
            {"semantic_unit_id": "lead", "sources": [{"member_id": "pm"}]}]))
    assert via_identity["retention_detail"] == "member detail"

    # An identity default outranks the member it draws from.
    identity_wins = prompt_context.effective_reference_config(
        {"source": {"semantic_unit_ids": ["lead"]}, "config": {}},
        capability, _member_tier_context(members, [
            {"semantic_unit_id": "lead", "sources": [{"member_id": "pm"}],
             "attachment_defaults": {"retention_detail": "identity detail"}}]))
    assert identity_wins["retention_detail"] == "identity detail"

    # A chip override still outranks everything below it.
    chip_wins = prompt_context.effective_reference_config(
        {"source": {"picture_ids": ["pm"]},
         "config": {"overrides": {"retention_detail": "chip detail"}}},
        capability, _member_tier_context(members))
    assert chip_wins["retention_detail"] == "chip detail"


def test_conflicting_member_defaults_fall_through_rather_than_first_wins():
    """Two selected members disagreeing must not let one win by ordering."""
    members = [
        {"member_id": "a", "attachment_defaults": {"retention_detail": "left"}},
        {"member_id": "b", "attachment_defaults": {"retention_detail": "right"}},
    ]
    config = prompt_context.effective_reference_config(
        {"source": {"picture_ids": ["a", "b"]}, "config": {}},
        {"capability_id": "retention", "kind": "retention"},
        _member_tier_context(members))
    assert config["retention_detail"] == "format detail"

    # Agreement inherits normally.
    agreed = [dict(member, attachment_defaults={"retention_detail": "same"})
              for member in members]
    config = prompt_context.effective_reference_config(
        {"source": {"picture_ids": ["a", "b"]}, "config": {}},
        {"capability_id": "retention", "kind": "retention"},
        _member_tier_context(agreed))
    assert config["retention_detail"] == "same"


def test_member_defaults_absent_leaves_the_format_default_alone():
    config = prompt_context.effective_reference_config(
        {"source": {"picture_ids": ["pm"]}, "config": {}},
        {"capability_id": "retention", "kind": "retention"},
        _member_tier_context([{"member_id": "pm"}]))
    assert config["retention_detail"] == "format detail"


def test_capability_enabled_absence_is_preserved_and_resolves_per_reference():
    """Absent means inherit, in storage and through the resolver.

    Coercing absence to True at the deserialization boundary meant no resolver
    downstream could ever see "inherit", so a shared default was inert.
    """
    sparse = prompt_context.normalize_capability(
        {"capability_id": "summary", "kind": "summary"})
    assert "enabled" not in sparse
    for stored in (True, False):
        explicit = prompt_context.normalize_capability(
            {"capability_id": "summary", "kind": "summary", "enabled": stored})
        assert explicit["enabled"] is stored

    context = {
        "profile": _MEMBER_TIER_PROFILE,
        "references": [{"reference_id": "ref", "members": [
            {"member_id": "pm", "disabled_capabilities": ["summary"]}]}],
        "semantic_units_by_id": {"lead": {
            "semantic_unit_id": "lead", "sources": [{"member_id": "pm"}],
            "disabled_capabilities": []}},
    }
    capability = {"capability_id": "summary", "kind": "summary"}

    # A physical selection follows its member.
    assert prompt_context._inherited_capability_enabled(
        {"source": {"picture_ids": ["pm"]}}, capability, context) is False
    # An identity that says nothing outranks the member it draws from, so the
    # part stays on: turning it off for the identity is a separate decision.
    assert prompt_context._inherited_capability_enabled(
        {"source": {"semantic_unit_ids": ["lead"]}}, capability, context) is True
    # A capability the Reference says nothing about stays on.
    assert prompt_context._inherited_capability_enabled(
        {"source": {"picture_ids": ["pm"]}},
        {"capability_id": "mentions", "kind": "mentions"}, context) is True


def test_disabled_capabilities_preserve_ids_from_another_format():
    """An id the active format does not declare must survive.

    One Reference can be used under several formats; dropping an unrecognised
    id would silently re-enable that part on the format that owns it.
    """
    assert prompt_context.normalize_disabled_capabilities(
        ["summary", "future_format_part", "summary", "", 5]) == [
            "summary", "future_format_part", "5"]
    assert prompt_context.normalize_disabled_capabilities(None) == []


def test_minimax_writing_aids_target_each_profiles_own_description_channel():
    """Base and Full Reference carry the same aids under different channel keys."""
    base = prompt_context.BUILTIN_PROFILES["minimax_h3_base@1"]["writing_aids"]
    ref = prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"]["writing_aids"]
    generic = prompt_context.BUILTIN_PROFILES["generic@1"]["writing_aids"]

    assert {key for aid in base for key in aid["channel_keys"]} == {
        "integrated_multimodal_description"}
    assert {key for aid in ref for key in aid["channel_keys"]} == {
        "detailed_description"}
    # One shared list could not have been right for both, and Generic imposes
    # no channel at all because its template names only `visual`.
    assert all("channel_keys" not in aid for aid in generic)


def test_h3_camera_motion_follows_its_own_documented_grammar():
    """Guide 4.3: motion type + amplitude + speed, both modifiers omissible."""
    base = {aid["id"]: aid for aid in
            prompt_context.BUILTIN_PROFILES["minimax_h3_base@1"]["writing_aids"]}
    generic = {aid["id"]: aid for aid in
               prompt_context.BUILTIN_PROFILES["generic@1"]["writing_aids"]}
    camera = base["camera_motion"]

    assert camera["text"] == "The camera {motion} {amplitude} {speed}."
    assert set(camera["fields"]) == {"motion", "amplitude", "speed"}
    # Twenty documented motion values, including the six the inherited Generic
    # list never carried.
    values = {choice["value"] for choice in camera["fields"]["motion"]["values"]}
    assert len(values) == 20
    assert {"pedestals up", "arcs around the subject", "tracks the subject",
            "shakes strongly", "takes the subject's point of view",
            "rolls clockwise"} <= values
    # "Add amplitude and speed only when they are meaningful."
    assert camera["fields"]["amplitude"]["optional"] is True
    assert camera["fields"]["speed"]["optional"] is True
    assert "optional" not in camera["fields"]["motion"]
    # MiniMax no longer inherits the Generic ten-verb list.
    assert generic["camera_motion"]["text"] == "The camera {motion}."
    assert len(generic["camera_motion"]["fields"]["motion"]["values"]) == 10


def test_framing_vocabulary_is_shared_and_not_claimed_as_provider_declared():
    """Neither H3 guide tables framing, so it is ours and belongs everywhere."""
    for key in ("generic@1", "minimax_h3_base@1", "minimax_h3_ref@1"):
        ids = {aid["id"] for aid
               in prompt_context.BUILTIN_PROFILES[key]["writing_aids"]}
        assert {"shot_distance", "shot_composition", "depth_of_field",
                "field_of_view"} <= ids
    depth = {aid["id"]: aid for aid in
             prompt_context.BUILTIN_PROFILES["generic@1"]["writing_aids"]
             }["depth_of_field"]
    # The focus change is a modifier, so it omits cleanly.
    assert depth["fields"]["focus"]["optional"] is True
    assert "optional" not in depth["fields"]["depth_of_field"]


def test_writing_aid_optional_flag_round_trips_and_rejects_a_non_boolean():
    profile = prompt_context.normalize_profile({
        "profile_id": "p", "version": "1", "name": "P", "template_id": "standard",
        "capabilities": {"shot": {"placement": "section_prefix"}},
        "writing_aids": [{"id": "a", "text": "x {v}", "fields": {
            "v": {"type": "enum", "optional": True, "values": ["one"]}}}],
    })
    assert profile["writing_aids"][0]["fields"]["v"]["optional"] is True
    # Writing-aid field shape is refused at normalization, the same gate that
    # already rejects a non-enum type or an empty vocabulary — an unsavable
    # declaration never reaches rest.
    with pytest.raises(ValueError, match="invalid_writing_aid_enum"):
        prompt_context.normalize_profile({
            "profile_id": "p", "version": "1", "name": "P",
            "template_id": "standard",
            "capabilities": {"shot": {"placement": "section_prefix"}},
            "writing_aids": [{"id": "a", "text": "x {v}", "fields": {
                "v": {"type": "enum", "optional": "yes", "values": ["one"]}}}],
        })


def test_removing_the_guide_binding_leaves_physical_delivery_intact():
    """The chip binding is gone; the compiler-composed alignment line is not.

    The binding described what `MiniMaxH3AddGuide` now delivers physically from
    the Guides Bridge. The picture-alignment instruction was never a chip's
    output — it is composed from the task mode — so it must survive untouched.
    """
    text = prompt_context.normalize_attachment({
        "kind": "custom", "config": {"text": "Use <Picture 1> as the pose"}})
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=[_section(0, 10, "move", attachments=[text])],
        window_start=0, window_end=10, fps=24, template="minimax_h3_base",
        context={"setup_manifest": {"setup": {"mode": "base", "task_mode": "I2VA"},
                                    "guides": [{"role": "first"}]}})
    assert compiled["prompt"].startswith(
        "For the target video, at 0.00 seconds into the target video, "
        "<Picture 1> (from [Shot 1]) is fully referenced.")
    # The three retired validators must not fire under any spelling.
    assert not {"invalid_h3_guide_role", "missing_h3_guide_binding",
                "unbound_h3_picture_guidance"} & {
                    value["code"] for value in compiled["errors"]}
    # Authored custom text still compiles as ordinary prose.
    assert "Use <Picture 1> as the pose" in compiled["prompt"]


def test_physical_guide_validation_still_belongs_to_the_setup():
    """`missing_first_guide` is the physical contract and is untouched."""
    resolved = minimax_h3.resolve_setup(
        setup={"mode": "base", "task_mode": "I2VA", "first_guide_id": "absent"},
        guide_frames=[],
        profile=prompt_context.BUILTIN_PROFILES["minimax_h3_base@1"])
    assert any(error["code"] == "missing_first_guide"
               for error in resolved["errors"])


def test_no_guide_binding_control_or_config_survives_anywhere():
    chips = (Path(__file__).resolve().parents[1]
             / "web" / "js" / "prompt_context_chips.js").read_text(encoding="utf-8")
    assert "setup_role" not in chips
    assert "Physical Guide binding" not in chips
    # Custom keeps its fixed-text control: it is the model-agnostic escape
    # hatch, not an H3 feature, and removing it would strip a capability from
    # every other provider on MiniMax's account.
    assert 'fieldRow("Fixed text", controls.text)' in chips


def _scope_scene(scope_attachment):
    """A source with text in BOTH channels, plus a consumer linking to it.

    Both channels must carry source text or "did not emit" would be ambiguous
    between "suppressed" and "there was nothing there".
    """
    source = PromptSection(
        start_frame=0, end_frame=10,
        channels={"visual": "earlier visual", "speech": "earlier speech"})
    source.prompt_id = "source"
    consumer = PromptSection(
        start_frame=10, end_frame=20,
        channels={"visual": "later text"}, attachments=[scope_attachment])
    consumer.prompt_id = "consumer"
    return [source, consumer]


def _compile_scope(scope_attachment, sections=None):
    return prompt_context.compile_prompt_context(
        global_channels={}, sections=sections or _scope_scene(scope_attachment),
        window_start=10, window_end=20, fps=24, template="sonder")


def test_scope_link_suppression_is_per_channel_not_all_or_nothing():
    link = prompt_context.normalize_attachment({
        "kind": "prompt_link_scope",
        "source": {"prompt_id": "source", "channel_keys": ["visual", "speech"]},
        "capabilities": [{
            "capability_id": prompt_context.scope_link_channel_capability_id("speech"),
            "kind": "prompt_link_scope", "enabled": False,
        }],
    })
    compiled = _compile_scope(link)
    rows = {row["channel_key"]: row
            for row in compiled["attachment_capability_projections"]
            if row["attachment_id"] == link["attachment_id"]}
    # Each selected channel gets its own row under its own capability id, which
    # is what the browser's suppression control writes to.
    assert set(rows) == {"visual", "speech"}
    assert rows["visual"]["capability_id"] == "prompt_link_scope:visual"
    assert rows["speech"]["capability_id"] == "prompt_link_scope:speech"
    # Only the muted channel stops emitting.
    assert rows["visual"]["state"] == "emitted"
    assert rows["speech"]["state"] != "emitted"


def test_a_legacy_scope_link_keeps_its_single_suppression_across_the_split():
    """A chip suppressed before per-channel capabilities existed stays off."""
    legacy = prompt_context.normalize_attachment({
        "kind": "prompt_link_scope",
        "source": {"prompt_id": "source", "channel_keys": ["visual", "speech"]},
        "capabilities": [{"capability_id": "prompt_link_scope",
                          "kind": "prompt_link_scope", "enabled": False}],
    })
    rows = [row for row in _compile_scope(legacy)["attachment_capability_projections"]
            if row["attachment_id"] == legacy["attachment_id"]]
    assert rows and not any(row["state"] == "emitted" for row in rows)

    # And an untouched legacy chip still emits every selected channel.
    plain = prompt_context.normalize_attachment({
        "kind": "prompt_link_scope",
        "source": {"prompt_id": "source", "channel_keys": ["visual", "speech"]},
    })
    plain_rows = {row["channel_key"]: row["state"] for row
                  in _compile_scope(plain)["attachment_capability_projections"]
                  if row["attachment_id"] == plain["attachment_id"]}
    assert plain_rows == {"visual": "emitted", "speech": "emitted"}


def test_per_channel_suppression_survives_a_transitive_link_chain():
    """Chaining through a muted channel must not resurrect it."""
    muted = prompt_context.normalize_attachment({
        "kind": "prompt_link_scope",
        "source": {"prompt_id": "first", "channel_keys": ["visual"]},
        "capabilities": [{
            "capability_id": prompt_context.scope_link_channel_capability_id("visual"),
            "kind": "prompt_link_scope", "enabled": False,
        }],
    })
    chained = prompt_context.normalize_attachment({
        "kind": "prompt_link_scope",
        "source": {"prompt_id": "middle", "channel_keys": ["visual"]},
    })
    sections = [
        _section(0, 10, "origin text", prompt_id="first"),
        _section(10, 20, "middle text", prompt_id="middle", attachments=[muted]),
        _section(20, 30, "last text", prompt_id="last", attachments=[chained]),
    ]
    compiled = prompt_context.compile_prompt_context(
        global_channels={}, sections=sections, window_start=20, window_end=30,
        fps=24, template="sonder")
    # The middle section's suppressed visual channel contributes nothing, so
    # the origin text must not arrive through the chain either.
    assert "origin text" not in compiled["prompt"]
    assert "middle text" in compiled["prompt"]


def test_labels_off_later_global_matches_final_prompt_and_relay():
    template = {
        "id": "standard", "name": "Labels off per channel",
        "channels": [{"key": "visual", "label": "Visual:"},
                     {"key": "speech", "label": "Speech:"}],
        "labels": "never", "global_merge": "per_channel",
        "global_channels_enabled": True,
        "default_context_profile": "generic@1",
    }
    compiled = prompt_context.compile_prompt_context(
        global_channels={"visual": "A", "speech": "B"},
        sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 10,
            "channels": {"visual": "LOCAL", "speech": ""},
            "global_channel_exceptions": ["visual"],
        }],
        window_start=0, window_end=10, fps=24,
        template=template, profile="generic@1")

    assert compiled["prompt"] == "B. LOCAL"
    assert compiled["relay"]["global_prompt"] == "B"
