import pytest

from server import prompt_context, prompt_tokens, routes
from server.timeline_state import (
    Asset, PromptSection, ReferenceEntity, ReferenceMember, Scene,
    TimelineProject,
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


def _handle_only_scene(text):
    """A scene whose ONLY Context is a handle typed in prose.

    No attachments anywhere, and no anchor nodes — which is the whole point:
    this is the shape that an anchors-only entry test cannot see.
    """
    project = _project()
    scene = Scene(scene_id="scene-1", name="Scene 1", duration_frames=120)
    scene.prompt_sections = [PromptSection(
        start_frame=0, end_frame=120,
        channel_docs={"visual": {"nodes": [{"type": "text", "text": text}]}})]
    project.scenes = [scene]
    return project, scene


def test_a_handle_only_scene_still_reaches_the_compiler(monkeypatch):
    """The entry predicate must count a typed handle as Context.

    `get_prompt_for_range` gates the compiler on attachments and document
    ANCHORS. Once a mention is plain text it is neither, so a handle-only scene
    was routed straight past compilation into `compose_range_prompt`, which
    reads the raw channel mirror and resolves nothing at all. The dormant node
    card would then print `@KWoman` while the executed render printed the
    resolved token — the two disagreeing about the same scene.

    Asserted through `get_prompt_for_range` deliberately. A test that called
    `compile_prompt_context` directly passes in every version of this code,
    including the broken one, because the defect is in whether the compiler is
    reached rather than in what it does.
    """
    project, scene = _handle_only_scene("a @KWoman walks past")
    assert not scene.global_attachments
    assert not any(section.attachments for section in scene.prompt_sections)
    assert not any(prompt_context.document_has_anchors(document)
                   for section in scene.prompt_sections
                   for document in section.channel_docs.values())

    # Asserted on whether the COMPILER RAN, not on the text. An unresolved
    # handle is left exactly as authored by design, so the compiled output and
    # the raw-mirror output are character-identical here — an assertion on the
    # text passes in both worlds and proves nothing. This one was measured
    # doing exactly that before it was rewritten.
    reached = []
    original = Scene.compile_prompt_context

    def spy(self, *args, **kwargs):
        reached.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Scene, "compile_prompt_context", spy)
    composed = scene.get_prompt_for_range(0, 120)
    assert reached, "a handle-only scene was routed past the compiler"
    # And the prose survives untouched, because this handle names a member that
    # is not staged in the window: never half-resolved.
    assert "@KWoman" in composed
    # The predicate itself, stated directly so a future refactor cannot satisfy
    # the assertion above by accident.
    assert prompt_context.document_has_handle_mentions(
        {"nodes": [{"type": "text", "text": "a @KWoman walks"}]})
    assert not prompt_context.document_has_handle_mentions(
        {"nodes": [{"type": "text", "text": "no mention here"}]})


def test_prose_with_no_handle_does_not_start_compiling(monkeypatch):
    """The predicate must not become "always compile".

    Widening it to every scene would put the compiler in front of prompts that
    have no Context at all, and `compose_range_prompt` is the cheaper path they
    are meant to take.
    """
    _, scene = _handle_only_scene("a woman walks past bob@example")
    # Spied, not merely asserted on the predicate: checking
    # `document_has_handle_mentions` alone passes with `has_context` hardcoded
    # true, which is exactly the degenerate case the docstring claims to rule
    # out. This is the mirror image of the test above.
    reached = []
    original = Scene.compile_prompt_context

    def spy(self, *args, **kwargs):
        reached.append(True)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Scene, "compile_prompt_context", spy)
    scene.get_prompt_for_range(0, 120)
    assert not reached, "a scene with no Context started compiling"


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
    assert restored["sources"] == [{
        "entity_id": "woman", "member_id": "portrait-member",
        "contribution": "appearance",
    }]
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


def _compile_chip_handle_prose(text):
    chip = prompt_context.normalize_attachment({
        "kind": "reference", "source": {"audio_ids": ["voice"]},
        "config": {"audio_relationship": text},
        "capabilities": [{
            "capability_id": "audio_relationship", "kind": "audio_relationship",
            "channel_key": "summary", "placement": "section_prefix",
        }],
    })
    context = {
        "setup_manifest": {
            "setup": {"mode": "reference"},
            "standalone_audios": [{"member_id": "voice", "audio_ordinal": 1}],
            "presentation": [{"kind": "audio", "member_id": "voice",
                              "audio_ordinal": 1}],
        },
        "ordinal_manifest": {
            "subjects": {"woman": 1}, "audios": {"voice": 1}},
        "unit_source_labels": {"woman": ["<Audio 1>"]},
        "unit_source_members": {"woman": ["voice"]},
        "semantic_units": [{
            "semantic_unit_id": "woman", "handle": "KWoman", "name": "Woman",
            "definition": "a woman", "sources": [
                {"entity_id": "woman-ref", "member_id": "voice"}],
        }],
    }
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(
            0, 24, channels={"detailed_description": "scene"},
            attachments=[chip])],
        window_start=0, window_end=24, fps=24,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context=context, labels_on=True)
    return chip, context, compiled


def test_handle_in_chip_config_prose_resolves_to_identity_only():
    chip, context, compiled = _compile_chip_handle_prose(
        "the voice timbre of @KWoman")
    assert compiled["channels"]["summary"] == (
        "the voice timbre of <Subject 1>")
    assert "<Subject 1> <Audio 1>" not in compiled["channels"]["summary"]
    capability = chip["capabilities"][0]
    context.update({
        "profile": prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"],
        "semantic_units_by_id": {"woman": context["semantic_units"][0]},
    })
    plan = prompt_context.copy_capability_plan(chip, capability, context)
    assert any(part.get("text") == "the voice timbre of @KWoman"
               for line in plan["lines"] for part in line["parts"])


def test_unresolved_handle_in_chip_prose_warns_without_blocking():
    chip, _context, compiled = _compile_chip_handle_prose(
        "the voice timbre of @Missing and @Missing")
    assert compiled["channels"]["summary"] == (
        "the voice timbre of @Missing and @Missing")
    assert compiled["errors"] == []
    diagnostics = [value for value in compiled["warnings"]
                   if value["code"] == "unresolved_handle_mention"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["attachment_id"] == chip["attachment_id"]
    assert diagnostics[0]["channel_key"] == "summary"


def test_chip_prose_handle_resolution_is_case_insensitive():
    _chip, _context, compiled = _compile_chip_handle_prose(
        "the voice timbre of @kWoMaN")
    assert compiled["channels"]["summary"] == (
        "the voice timbre of <Subject 1>")


def test_inherited_definition_handles_resolve_at_line_seam_but_copy_stays_live():
    chip = prompt_context.normalize_attachment({
        "attachment_id": "definition-chip", "kind": "reference",
        "source": {"semantic_unit_ids": ["woman"]},
        "capabilities": [{
            "capability_id": "definitions", "kind": "definitions",
            "channel_key": "subject_definitions", "placement": "section_prefix",
        }],
    })
    unit = {
        "semantic_unit_id": "woman", "handle": "KWoman", "name": "Woman",
        "kind": "subject", "sources": [
            {"entity_id": "portrait-ref", "member_id": "portrait"}],
    }
    compiled = prompt_context.compile_prompt_context(
        sections=[PromptSection(
            0, 24, channels={"detailed_description": "scene"},
            attachments=[chip])],
        window_start=0, window_end=24, fps=24,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": {
                "setup": {"mode": "reference"},
                "pictures": [{"member_id": "portrait", "picture_ordinal": 1}],
                "presentation": [{
                    "kind": "picture", "member_id": "portrait",
                    "picture_ordinal": 1,
                    "member_prompt": (
                        "portrait of @KWoman beside @Missing and @Missing"),
                }],
            },
            "ordinal_manifest": {
                "subjects": {"woman": 1}, "pictures": {"portrait": 1}},
            "unit_source_labels": {"woman": ["<Picture 1>"]},
            "unit_source_members": {"woman": ["portrait"]},
            "semantic_units": [unit],
        }, labels_on=True,
        copy_plan_for={
            "attachment_id": "definition-chip",
            "capability_id": "definitions",
        })
    rendered = compiled["channels"]["subject_definitions"]
    assert "portrait of <Subject 1> beside @Missing and @Missing" in rendered
    diagnostics = [value for value in compiled["warnings"]
                   if value["code"] == "unresolved_handle_mention"]
    assert len(diagnostics) == 1
    assert diagnostics[0]["attachment_id"] == "definition-chip"
    assert diagnostics[0]["channel_key"] == "subject_definitions"
    copy_text = "".join(
        part.get("text", "")
        for line in compiled["copy_plan"]["lines"]
        for part in line["parts"])
    assert "portrait of @KWoman beside @Missing and @Missing" in copy_text


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
