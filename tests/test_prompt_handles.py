import pytest

from server import prompt_context, prompt_tokens, routes
from server.timeline_state import (
    Asset, PromptSection, ReferenceEntity, ReferenceMember, TimelineProject,
)


def _project():
    project = TimelineProject(project_id="handles")
    project.assets = [Asset(
        asset_id="portrait", name="Portrait", asset_type="image",
        path="media/portrait.png")]
    project.references = [ReferenceEntity(
        reference_id="woman", name="Korean Woman", members=[ReferenceMember(
            member_id="portrait-member", asset_id="portrait", handle="KWoman")])]
    return project


def test_reference_member_handle_and_prompt_defaults_round_trip():
    member = ReferenceMember(
        member_id="member", asset_id="portrait", handle="KWoman",
        visual_intent="partial", audio_intent="copy_partial")
    restored = ReferenceMember.from_dict(member.to_dict())
    assert restored.to_dict() == member.to_dict()


def test_identity_contributions_and_attachment_defaults_survive_round_trip():
    raw = {
        "semantic_unit_id": "lead", "handle": "Lead", "name": "Lead",
        "sources": [{
            "entity_id": "woman", "member_id": "portrait-member",
            "contribution": "appearance", "inherit_description": True,
        }],
        "attachment_defaults": {
            "summary": "Default summary", "retention_detail": "Keep the coat",
            "audio_definition": "Dry studio voice",
        },
    }
    normalized = prompt_context.normalize_semantic_unit(raw)
    restored = prompt_context.normalize_semantic_unit(normalized)
    assert restored == normalized
    assert restored["sources"] == raw["sources"]
    assert restored["attachment_defaults"] == raw["attachment_defaults"]


def test_identity_intents_stay_sparse_and_preserve_unknown_authored_values():
    bare = prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "base", "name": "Base identity"})
    assert "visual_intent" not in bare
    assert "audio_intent" not in bare

    future = prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "future", "name": "Future identity",
        "visual_intent": "future_visual",
        "audio_intent": "future_audio",
    })
    assert future["visual_intent"] == "future_visual"
    assert future["audio_intent"] == "future_audio"
    assert prompt_context.normalize_semantic_unit(future) == future


def test_handle_namespace_is_case_insensitive_across_physical_and_identity():
    project = _project()
    project.prompt_semantic_units = [prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "lead", "handle": "Lead", "name": "Lead",
    })]

    with pytest.raises(routes.ProjectMutationRequestError) as physical_collision:
        routes._apply_reference_mutation_operations(project, [{
            "type": "create_member", "reference_id": "woman",
            "fields": {
                "asset_id": "portrait", "handle": "lead", "prompt": "",
                "name": "", "tags": [], "crop": None,
                "source_start_sec": 0, "source_end_sec": None,
            },
        }])
    assert physical_collision.value.code == "handle_collision"

    units = [prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "other", "handle": "kwoman", "name": "Other",
    })]
    with pytest.raises(routes.ProjectMutationRequestError) as identity_collision:
        routes._require_prompt_handle_available(
            project, "kwoman", "prompt identity", "other",
            semantic_units=units)
    assert identity_collision.value.code == "handle_collision"


def test_member_update_and_delete_expected_use_stored_handle_not_suggestion():
    project = _project()
    member = project.references[0].members[0]
    routes._apply_reference_mutation_operations(project, [{
        "type": "update_member", "reference_id": "woman",
        "member_id": member.member_id,
        "fields": {"handle": "KoreanWoman"},
        "expected": {"handle": "KWoman"},
    }])
    assert member.handle == "KoreanWoman"

    stale = member.to_dict()
    stale["handle"] = "KWoman"
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_reference_mutation_operations(project, [{
            "type": "delete_member", "reference_id": "woman",
            "member_id": member.member_id, "expected": stale,
        }])
    assert refused.value.code == "identity_mismatch"
    routes._apply_reference_mutation_operations(project, [{
        "type": "delete_member", "reference_id": "woman",
        "member_id": member.member_id, "expected": member.to_dict(),
    }])
    assert project.references[0].members == []


def test_first_use_materializes_and_deduplicates_inside_reference_batch():
    project = _project()
    project.references[0].members.append(ReferenceMember(
        member_id="blank-member", asset_id="portrait", handle=""))
    result = routes._apply_reference_mutation_operations(project, [{
        "type": "materialize_member_handle", "reference_id": "woman",
        "member_id": "blank-member", "suggestion": "KWoman",
        "expected": {"handle": ""},
    }])
    assert result["results"][0]["handle"] == "KWoman2"
    assert project.references[0].members[1].handle == "KWoman2"


def test_first_use_refuses_stale_stored_handle_without_partial_change():
    project = _project()
    member = project.references[0].members[0]
    with pytest.raises(routes.ProjectMutationRequestError) as refused:
        routes._apply_reference_mutation_operations(project, [{
            "type": "materialize_member_handle", "reference_id": "woman",
            "member_id": member.member_id, "suggestion": "Other",
            "expected": {"handle": ""},
        }])
    assert refused.value.code == "identity_mismatch"
    assert member.handle == "KWoman"


def test_reference_config_sparse_override_reset_empty_and_capability_precedence():
    unit = prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "lead", "handle": "Lead", "name": "Lead",
        "attachment_defaults": {"summary": "Identity default"},
    })
    context = {
        "profile": {"capabilities": {"reference": {
            "defaults": {"summary": "Format default"},
        }}},
        "semantic_units_by_id": {"lead": unit},
    }
    attachment = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"semantic_unit_ids": ["lead"]},
    })
    capability = {"capability_id": "summary", "kind": "summary", "config": {}}
    assert prompt_context.effective_reference_config(
        attachment, capability, context)["summary"] == "Identity default"

    unit["attachment_defaults"]["summary"] = "Changed default"
    assert prompt_context.effective_reference_config(
        attachment, capability, context)["summary"] == "Changed default"

    attachment["config"]["overrides"]["summary"] = "Chip override"
    unit["attachment_defaults"]["summary"] = "Changed again"
    assert prompt_context.effective_reference_config(
        attachment, capability, context)["summary"] == "Chip override"

    attachment["config"]["overrides"]["summary"] = ""
    assert prompt_context.effective_reference_config(
        attachment, capability, context)["summary"] == ""
    del attachment["config"]["overrides"]["summary"]
    assert prompt_context.effective_reference_config(
        attachment, capability, context)["summary"] == "Changed again"

    capability["config"] = {"summary": "Capability override"}
    attachment["config"]["overrides"]["summary"] = "Chip override"
    assert prompt_context.effective_reference_config(
        attachment, capability, context)["summary"] == "Capability override"


def test_flat_reference_config_migrates_once_to_sparse_overrides():
    attachment = prompt_context.normalize_attachment({
        "kind": "reference",
        "config": {"summary": "Old shape", "audio_speaker_subject_id": "lead"},
    })
    assert attachment["config"] == {
        "audio_speaker_subject_id": "lead",
        "overrides": {"summary": "Old shape"},
    }


def test_handle_rename_does_not_change_authored_stable_token():
    declarations = prompt_context.prompt_token_declarations(
        prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"])
    authored = prompt_tokens.token("picture", "portrait-member", declarations)
    assert authored == "@picture(portrait-member)"
    project = _project()
    project.references[0].members[0].handle = "RenamedWoman"
    resolved, unresolved = prompt_tokens.resolve(
        authored, {"pictures": {"portrait-member": 2}}, declarations=declarations)
    assert (resolved, unresolved) == ("<Picture 2>", [])
    assert authored == "@picture(portrait-member)"


def test_duplicate_staging_blocks_only_when_the_member_is_referenced():
    duplicate_slots = {
        "portrait-member": [
            {"population": "pictures", "slot_id": "lane-a:item:portrait-member",
             "lane_id": "lane-a", "ordinal": 1, "label": "<Picture 1>"},
            {"population": "pictures", "slot_id": "lane-b:item:portrait-member",
             "lane_id": "lane-b", "ordinal": 2, "label": "<Picture 2>"},
        ],
    }
    context = {
        "setup_manifest": {
            "setup": {"mode": "reference"},
            "duplicate_member_slots": duplicate_slots,
        },
        "ordinal_manifest": {"pictures": {"portrait-member": 1}},
    }
    unreferenced = prompt_context.compile_prompt_context(
        global_channels={}, sections=[PromptSection(
            0, 10, channels={"detailed_description": "A room."})],
        window_start=0, window_end=10, fps=24,
        template="minimax_h3_ref", context=context)
    assert "ambiguous_physical_handle" not in {
        value["code"] for value in unreferenced["errors"]}

    attachment = prompt_context.normalize_attachment({
        "kind": "custom",
        "config": {"text": "Use @picture(portrait-member)"},
    })
    referenced = prompt_context.compile_prompt_context(
        global_channels={}, sections=[PromptSection(
            0, 10, channels={"detailed_description": "A room."},
            attachments=[attachment])],
        window_start=0, window_end=10, fps=24,
        template="minimax_h3_ref", context=context)
    error = next(value for value in referenced["errors"]
                 if value["code"] == "ambiguous_physical_handle")
    assert "lane-a" in error["message"] and "lane-b" in error["message"]
