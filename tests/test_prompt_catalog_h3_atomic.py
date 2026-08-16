"""Server-owned Prompt catalog and atomic MiniMax H3 population tests."""

import copy
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from server import minimax_h3, prompt_context, routes
from server.project_manager import ProjectVersionConflict
from server.timeline_state import LaneConfig, ReferenceLaneRecipe, Scene, TimelineProject


def _blank_scene():
    return Scene(
        scene_id="scene", reference_lane_count=1,
        reference_lane_configs=[LaneConfig()],
        reference_lane_recipes=[ReferenceLaneRecipe(lane_id="blank")])


def _ensure(scene, population="picture"):
    return routes._ensure_minimax_h3_reference_population(
        scene, {"type": "ensure_minimax_h3_reference_population",
                "population": population})


def test_catalog_is_versioned_and_fork_seeds_exclude_server_owned_fields():
    catalog = routes._references_payload(TimelineProject(
        project_id="project"))["prompt_context_catalog"]
    assert catalog["schema_version"] == 1
    assert "role_aliases" not in catalog
    assert "profile_templates" not in catalog
    assert "writing_aids" not in catalog
    assert catalog["profiles"]
    reserved = prompt_context.PROFILE_RESERVED_DEFINITION_FIELDS
    for descriptor in catalog["profiles"]:
        assert not reserved.intersection(descriptor["fork_seed"])
        assert set(descriptor["fork_seed"]).issubset(
            prompt_context.PROFILE_DEFINITION_FIELDS)


def test_catalog_publishes_format_owned_population_identity_and_speaker_declarations():
    catalog = routes._references_payload(TimelineProject(
        project_id="project"))["prompt_context_catalog"]
    descriptors = {row["key"]: row for row in catalog["profiles"]}
    reference = descriptors["minimax_h3_ref@1"]["fork_seed"]
    base = descriptors["minimax_h3_base@1"]["fork_seed"]

    assert [row["key"] for row in reference["physical_populations"]] == [
        "pictures", "videos", "standalone_audios"]
    assert reference["identity_kinds"] == [prompt_context.MINIMAX_SUBJECT_KIND]
    assert base["physical_populations"] == []
    assert base["identity_kinds"][0]["referenced_label_template"] == ""
    assert reference["speaker_policy"]["token_template"] == "(S{n})"


def test_catalog_publishes_resolved_derived_declarations_and_phases_without_globals():
    catalog = routes._references_payload(TimelineProject(
        project_id="project"))["prompt_context_catalog"]
    descriptors = {row["key"]: row for row in catalog["profiles"]}
    resolved = descriptors["minimax_h3_ref@1"]["resolved"]
    derived = resolved["capabilities"]["reference"]["derived"]
    assert [name for name, _value in sorted(
        derived.items(), key=lambda item: item[1]["order"])] == [
            "definitions", "summary", "retention", "mentions",
            "audio_relationship"]
    task_types = derived["summary"]["fields"]["task_types"]
    assert task_types["type"] == "enum_multi"
    assert [row["value"] for row in task_types["values"]] == list(
        prompt_context.MINIMAX_TASK_TYPES)
    assert task_types["default_source"] == "roles"
    assert resolved["contribution_catalog"]
    assert resolved["speaker_policy"]["token_template"] == "(S{n})"
    assert [row["value"] for row in catalog["placement_phases"]] == list(
        prompt_context.PLACEMENT_PHASES)
    assert "capabilities" not in catalog
    assert "role_catalogs" not in catalog
    assert "minimax_task_types" not in catalog


def test_h3_derived_routes_match_the_reviewed_legacy_route_golden():
    derived = prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"][
        "capabilities"]["reference"]["derived"]
    assert {kind: value["channel_key"] for kind, value in derived.items()} == {
        "definitions": "subject_definitions",
        "summary": "summary",
        "retention": "retention_analysis",
        "mentions": "detailed_description",
        "audio_relationship": "summary",
    }
    assert {kind: value["placement"] for kind, value in derived.items()} == {
        "definitions": "section_prefix",
        "summary": "section_prefix",
        "retention": "section_prefix",
        "mentions": "inline",
        "audio_relationship": "section_prefix",
    }


def test_h3_derived_compiler_matches_the_reviewed_pre_migration_semantic_golden():
    """Pin the five semantic outputs promised by the Phase A migration gate."""
    def chip(attachment_id, kind, placement, config):
        return prompt_context.normalize_attachment({
            "attachment_id": attachment_id,
            "emission_group_id": attachment_id,
            "kind": "reference",
            "source": {"semantic_unit_ids": ["subject"]},
            "config": config,
            "capabilities": [{
                "capability_id": kind, "kind": kind,
                "placement": placement,
            }],
        })

    attachments = [
        chip("definition-chip", "definitions", "section_prefix",
             {"definition": "a poised woman"}),
        chip("summary-chip", "summary", "section_prefix", {
            "summary": "She crosses the room.",
            "task_types": ["reference generation"],
        }),
        chip("retention-chip", "retention", "section_prefix", {
            "retention_detail": "face and blue coat",
            "visual_intent": "preserve",
        }),
        chip("mention-chip", "mentions", "inline", {
            "text": "<Subject 1> turns.",
        }),
    ]
    setup_manifest = {
        "setup": {"mode": "reference", "setup_id": "setup"},
        "pictures": [{
            "member_id": "portrait", "role": "identity",
            "picture_ordinal": 1,
        }],
        "videos": [],
        "standalone_audios": [],
        "presentation": [{
            "kind": "picture", "member_id": "portrait",
            "picture_ordinal": 1,
        }],
    }
    compiled = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 24,
            "channel_docs": {"detailed_description": {
                "schema": "prompt_document_v1",
                "nodes": [
                    {"type": "text", "node_id": "before", "text": "Before "},
                    {"type": "attachment", "node_id": "mention",
                     "attachment_id": "mention-chip",
                     "capability_id": "mentions"},
                    {"type": "text", "node_id": "after", "text": " after."},
                ],
            }},
            "attachments": attachments,
        }],
        window_start=0, window_end=24, fps=24,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": setup_manifest,
            "ordinal_manifest": {
                "subjects": {"subject": 1},
                "pictures": {"portrait": 1}, "videos": {}, "audios": {},
            },
            "unit_source_labels": {"subject": ["<Picture 1>"]},
            "semantic_units": [{
                "semantic_unit_id": "subject", "name": "Woman",
                "definition": "a poised woman",
                "sources": [{
                    "entity_id": "woman", "member_id": "portrait",
                }],
            }],
        },
        labels_on=True,
    )
    expected_channels = {
        "subject_definitions": (
            "<Subject 1> is a poised woman from <Picture 1>"),
        "summary": "[reference generation] She crosses the room.",
        "retention_analysis": (
            "<Subject 1>: fully_preserved - face and blue coat"),
        "detailed_description": "Before <Subject 1> turns. after.",
        "overall_soundscape": "",
        "non_diegetic_music": "",
    }
    expected_prompt = (
        "subject_definitions:\n"
        "<Subject 1> is a poised woman from <Picture 1>\n\n"
        "summary:\n[reference generation] She crosses the room.\n\n"
        "retention_analysis:\n"
        "<Subject 1>: fully_preserved - face and blue coat\n\n"
        "detailed_description:\nBefore <Subject 1> turns. after."
    )
    expected_emissions = [
        {"attachment_id": "mention-chip", "capability_id": "mentions",
         "channel_key": "detailed_description",
         "emission_group_id": "mention-chip", "kind": "reference",
         "origin": "section", "placement": "inline",
         "text": "<Subject 1> turns."},
        {"attachment_id": "definition-chip", "capability_id": "definitions",
         "channel_key": "subject_definitions",
         "emission_group_id": "definition-chip", "kind": "reference",
         "origin": "section", "placement": "section_prefix",
         "text": "<Subject 1> is a poised woman from <Picture 1>"},
        {"attachment_id": "summary-chip", "capability_id": "summary",
         "channel_key": "summary", "emission_group_id": "summary-chip",
         "kind": "reference", "origin": "section",
         "placement": "section_prefix",
         "text": "[reference generation] She crosses the room."},
        {"attachment_id": "retention-chip", "capability_id": "retention",
         "channel_key": "retention_analysis",
         "emission_group_id": "retention-chip", "kind": "reference",
         "origin": "section", "placement": "section_prefix",
         "text": "<Subject 1>: fully_preserved - face and blue coat"},
    ]
    assert {key: compiled[key] for key in (
        "prompt", "channels", "segments", "emissions", "setup_manifest",
    )} == {
        "prompt": expected_prompt,
        "channels": expected_channels,
        "segments": [{
            "start": 0, "end": 24, "section_start": 0,
            "prompt_id": "section", "channels": expected_channels,
            "text": expected_prompt, "global_channel_exceptions": [],
            "_opens_shot": False, "_shot_timestamp": False,
        }],
        "emissions": expected_emissions,
        "setup_manifest": setup_manifest,
    }


def test_legacy_routes_stay_normalizable_but_are_refused_as_declarations():
    """The pre-`derived` routing shape is tolerated at rest and refused as a
    declaration.

    The dedicated migration action was removed once no project in circulation
    could hold this shape, so the refusal now arrives as the ordinary
    `incomplete_capability_declaration` rather than a bespoke code. Tolerance
    still matters: `normalize_profile` must not raise, or a frozen prompt-history
    envelope carrying such a profile becomes unrestorable.
    """
    raw = {
        "profile_id": "legacy", "version": "1", "name": "Legacy",
        "template_id": "standard", "writing_aids": [],
        "capabilities": {"reference": {
            "routes": {"derived_prompt": "visual"}}},
    }
    normalized = prompt_context.normalize_profile(raw)
    assert normalized["capabilities"]["reference"]["routes"] == {
        "derived_prompt": "visual"}
    codes = [row["code"] for row in
             prompt_context.profile_declaration_errors(normalized)]
    assert codes == ["incomplete_capability_declaration"]
    with pytest.raises(prompt_context.ProfileResolutionError) as exc:
        prompt_context.resolve_profile(normalized, template="standard")
    assert exc.value.code == "invalid_profile_declaration"
    # Pass-through tolerant, so an unrelated profile-list save still succeeds
    # and the author can repair the format through Edit as new version.
    project = TimelineProject(project_id="project",
                              prompt_context_profiles=[normalized])
    assert routes._normalize_prompt_context_profile_update(
        project, [normalized]) == [normalized]


def test_profile_usage_scan_tolerates_a_blank_reference_lane_recipe():
    """A blank lane stores `{}`, so `.get("soft")` is None, not a dict.

    Treating it as a dict raised AttributeError inside the usage scan, which
    aiohttp turned into a 500 — deleting a custom format was unreachable in any
    project holding one default Reference lane.
    """
    scene = Scene(scene_id="scene", reference_lane_count=1,
                  reference_lane_configs=[LaneConfig()],
                  reference_lane_recipes=[ReferenceLaneRecipe(lane_id="blank",
                                                              recipe={})])
    custom = prompt_context.normalize_profile({
        "profile_id": "custom", "version": "1", "name": "Custom",
        **prompt_context.profile_fork_seed(
            prompt_context.BUILTIN_PROFILES["generic@1"])})
    project = TimelineProject(project_id="project", scenes=[scene],
                              prompt_context_profiles=[custom])

    assert routes._prompt_context_profile_usages(project, "custom@1") == []
    # The delete path is the one the ⋯ menu drives, and it must reach the
    # normal refusal/acceptance contract rather than raising.
    assert routes._normalize_prompt_context_profile_update(project, []) == []

    # A recipe explicitly storing `soft: None` is equally tolerated.
    scene.reference_lane_recipes[0].recipe = {"soft": None}
    assert routes._prompt_context_profile_usages(project, "custom@1") == []

    # Anti-vacuity: a real usage is still reported.
    scene.reference_lane_recipes[0].recipe = {
        "soft": {"compatible_profiles": ["custom@1"]}}
    assert [row["type"] for row in
            routes._prompt_context_profile_usages(project, "custom@1")] == [
        "reference_recipe"]


@pytest.mark.parametrize("definition,expected", [
    # The reported defect: inherited Library prose ends in a full stop, so the
    # citation landed after it as a fragment.
    ("a poised woman in a blue coat.",
     "<Subject 1> is a poised woman in a blue coat from <Picture 1>"),
    ("a poised woman in a blue coat.  ",
     "<Subject 1> is a poised woman in a blue coat from <Picture 1>"),
    # No terminal punctuation is the existing shape and must not change.
    ("a poised woman",
     "<Subject 1> is a poised woman from <Picture 1>"),
    # Dropping these would change the meaning, not the punctuation.
    ("Is she the one?",
     "<Subject 1> is Is she the one? from <Picture 1>"),
    ("What a coat!",
     "<Subject 1> is What a coat! from <Picture 1>"),
])
def test_subject_definition_weaves_its_source_citation(definition, expected):
    compiled = prompt_context.compile_prompt_context(
        global_channels={},
        sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 24,
            "channels": {}, "attachments": [prompt_context.normalize_attachment({
                "attachment_id": "chip", "kind": "reference",
                "source": {"semantic_unit_ids": ["subject"]},
                "config": {"definition": definition},
                "capabilities": [{"capability_id": "definitions",
                                  "kind": "definitions"}],
            })],
        }],
        window_start=0, window_end=24, fps=24,
        template="minimax_h3_ref", profile="minimax_h3_ref@1",
        context={
            "setup_manifest": {
                "setup": {"mode": "reference", "setup_id": "setup"},
                "pictures": [{"member_id": "portrait", "role": "identity",
                              "picture_ordinal": 1}],
                "videos": [], "standalone_audios": [],
                "presentation": [{"kind": "picture", "member_id": "portrait",
                                  "picture_ordinal": 1}],
            },
            "ordinal_manifest": {"subjects": {"subject": 1},
                                 "pictures": {"portrait": 1},
                                 "videos": {}, "audios": {}},
            "unit_source_labels": {"subject": ["<Picture 1>"]},
            "semantic_units": [{
                "semantic_unit_id": "subject", "name": "Woman",
                "definition": definition,
                "sources": [{"entity_id": "woman", "member_id": "portrait"}],
            }],
        },
        labels_on=True,
    )
    assert compiled["channels"]["subject_definitions"] == expected


def test_normalization_stays_tolerant_so_frozen_envelopes_re_import():
    """A frozen prompt-history entry carrying any stored profile must re-import.

    Normalization is deliberately pass-through: it never raises on a shape it
    does not recognise, and never rewrites one. Tightening it would make every
    history entry captured under that shape unrestorable, and would also make
    the at-rest copy differ from the normalized import, so every re-import would
    conflict.
    """
    legacy = prompt_context.normalize_profile({
        "profile_id": "legacy", "version": "1", "name": "Legacy",
        "template_id": "standard", "writing_aids": [],
        "capabilities": {"reference": {
            "placement": "section_prefix",
            "routes": {"summary": "visual", "definitions": "visual"}}},
    })
    assert prompt_context.normalize_profile(legacy) == legacy
    restored = TimelineProject(project_id="restored",
                               prompt_context_profiles=[legacy])
    assert routes._normalize_prompt_context_profile_update(
        restored, [legacy]) == [legacy]


def test_derived_declaration_validation_covers_defaults_channels_and_depth():
    seed = prompt_context.profile_fork_seed(
        prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"])
    raw = {"profile_id": "custom", "version": "1", "name": "Custom", **seed}
    # The enriched built-in reaches the old bound and must normalize intact.
    normalized = prompt_context.normalize_profile(raw)
    assert normalized["capabilities"]["reference"]["derived"]

    invalid_source = copy.deepcopy(normalized)
    field = invalid_source["capabilities"]["reference"]["derived"][
        "definitions"].setdefault("fields", {})
    field["mode"] = {"type": "enum", "values": ["one"],
                     "default_source": "browser_guess"}
    assert "unknown_field_default_source" in {
        row["code"] for row in
        prompt_context.profile_declaration_errors(invalid_source)}

    invalid_channel = copy.deepcopy(normalized)
    invalid_channel["capabilities"]["reference"]["derived"][
        "definitions"]["channel_key"] = "missing"
    assert "incomplete_capability_declaration" in {
        row["code"] for row in prompt_context.profile_declaration_errors(
            invalid_channel,
            template={"id": "minimax_h3_ref",
                      "channels": [{"key": "visual"}]})}

    malformed = copy.deepcopy(normalized)
    malformed["capabilities"]["reference"]["derived"]["summary"][
        "fields"]["task_types"]["default_source"] = {}
    malformed["speaker_policy"]["compound_order"] = {}
    codes = {row["code"] for row in
             prompt_context.profile_declaration_errors(malformed)}
    assert "unknown_field_default_source" in codes
    assert "invalid_speaker_policy" in codes

    divergent_field = copy.deepcopy(raw)
    divergent_field["writing_aids"][0]["fields"]["language"][
        "default_source"] = "browser_guess"
    with pytest.raises(ValueError, match="invalid_writing_aid_enum"):
        prompt_context.normalize_profile(divergent_field)


def test_profile_write_path_validates_the_bound_channel_template():
    project = TimelineProject(project_id="project")
    seed = prompt_context.profile_fork_seed(
        prompt_context.BUILTIN_PROFILES["generic@1"])
    seed["capabilities"]["reference"]["derived"]["derived_prompt"][
        "channel_key"] = "missing"
    with pytest.raises(routes.ProjectMutationRequestError):
        routes._create_prompt_context_profile(project, {
            "profile_id": "bad_channel", "version": "1",
            "name": "Bad channel", "definition": seed,
        })

    multi_template = prompt_context.profile_fork_seed(
        prompt_context.BUILTIN_PROFILES["generic@1"])
    multi_template["compatible_templates"] = ["minimax_h3_ref"]
    with pytest.raises(routes.ProjectMutationRequestError) as exc:
        routes._create_prompt_context_profile(project, {
            "profile_id": "bad_compatible_channel", "version": "1",
            "name": "Bad compatible channel", "definition": multi_template,
        })
    assert exc.value.code == "invalid_prompt_context_profile"

    custom_template = {
        "id": "custom-template", "channels": [{"key": "only"}],
        "default_context_profile": "custom@1",
    }
    custom = {"profile_id": "custom", "version": "1", "name": "Custom",
              **prompt_context.profile_fork_seed(
                  prompt_context.BUILTIN_PROFILES["generic@1"])}
    assert "incomplete_capability_declaration" in {
        row["code"] for row in prompt_context.profile_declaration_errors(
            custom, template=custom_template)}


def _compile_reference_with(capability, *, profile="generic@1", template="standard",
                            config=None, context=None):
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "reference-chip", "kind": "reference",
        "source": {"reference_item_id": "item"},
        "config": config or {}, "capabilities": [capability],
    })
    return prompt_context.compile_prompt_context(
        global_channels={}, sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 10,
            "channels": {"visual": ""}, "attachments": [attachment],
        }], window_start=0, window_end=10, template=template, profile=profile,
        context={"generic_references": {"item": {"prompt": "reference text"}},
                 **(context or {})})


def test_unknown_placement_and_reference_capability_are_preserved_and_blocked():
    unknown_placement = prompt_context.normalize_capability({
        "capability_id": "derived_prompt", "kind": "derived_prompt",
        "placement": "future_phase", "enabled": True,
    })
    assert unknown_placement["placement"] == "future_phase"
    compiled = _compile_reference_with(unknown_placement)
    assert "invalid_attachment_placement" in {
        row["code"] for row in compiled["errors"]}
    assert "reference text" not in compiled["prompt"]

    undeclared = _compile_reference_with({
        "capability_id": "mystery", "kind": "mystery",
        "placement": "section_prefix", "enabled": True, "config": {},
    })
    assert "undeclared_reference_capability" in {
        row["code"] for row in undeclared["errors"]}
    assert "reference text" not in undeclared["prompt"]

    declared_but_unrenderable = copy.deepcopy(
        prompt_context.BUILTIN_PROFILES["generic@1"])
    declared_but_unrenderable["capabilities"]["reference"]["derived"] = {
        "mystery": {"order": 1, "channel_key": "visual",
                    "placement": "inline", "label": "Mystery", "fields": {}}}
    assert "incomplete_capability_declaration" in {
        row["code"] for row in prompt_context.profile_declaration_errors(
            declared_but_unrenderable)}


def test_missing_capability_placement_inherits_the_format_default_at_its_anchor():
    attachment = prompt_context.normalize_attachment({
        "attachment_id": "reference-chip", "kind": "reference",
        "source": {"reference_item_id": "item"},
        "capabilities": [{"capability_id": "derived_prompt",
                          "kind": "derived_prompt", "enabled": True}],
    })
    assert attachment["capabilities"][0]["placement"] == ""
    compiled = prompt_context.compile_prompt_context(
        sections=[{
            "prompt_id": "section", "start_frame": 0, "end_frame": 10,
            "channel_docs": {"visual": {"schema": "prompt_document_v1", "nodes": [
                {"type": "text", "node_id": "before", "text": "before "},
                {"type": "attachment", "node_id": "anchor",
                 "attachment_id": "reference-chip",
                 "capability_id": "derived_prompt"},
                {"type": "text", "node_id": "after", "text": " after"},
            ]}}, "attachments": [attachment],
        }], window_start=0, window_end=10, template="standard",
        context={"generic_references": {"item": {"prompt": "REF"}}})
    assert compiled["channels"]["visual"] == "before REF after"
    projection = next(row for row in compiled["attachment_capability_projections"]
                      if row["capability_id"] == "derived_prompt")
    assert projection["declared_placement"] == "inline"


def test_declared_task_type_vocabulary_orders_explicit_values_and_blocks_unknowns():
    profile = copy.deepcopy(prompt_context.BUILTIN_PROFILES["minimax_h3_ref@1"])
    choices = profile["capabilities"]["reference"]["derived"]["summary"][
        "fields"]["task_types"]["values"]
    choices[:] = [
        {"value": "audio reference", "label": "Audio"},
        {"value": "reference generation", "label": "Reference"},
    ]
    assert prompt_context._minimax_task_types(
        {}, ["reference generation", "audio reference"], profile) == [
            "audio reference", "reference generation"]
    assert prompt_context._minimax_task_types({}, profile=None) == []

    attachment = prompt_context.normalize_attachment({
        "attachment_id": "summary", "kind": "reference",
        "source": {"semantic_unit_ids": []},
        "capabilities": [{
            "capability_id": "summary", "kind": "summary",
            "placement": "section_prefix", "enabled": True,
            "config": {"task_types": ["not declared"]},
        }],
    })
    compiled = prompt_context.compile_prompt_context(
        sections=[{"prompt_id": "section", "start_frame": 0, "end_frame": 10,
                   "channels": {}, "attachments": [attachment]}],
        window_start=0, window_end=10, template="minimax_h3_ref",
        context={"setup_manifest": {"setup": {"mode": "reference"}}})
    assert "unknown_declared_field_value" in {
        row["code"] for row in compiled["errors"]}

    retention = prompt_context.normalize_attachment({
        "attachment_id": "retention", "kind": "reference",
        "source": {"picture_ids": ["picture"]},
        "capabilities": [{
            "capability_id": "retention", "kind": "retention",
            "placement": "section_prefix", "enabled": True,
            "config": {"visual_intent": "future"},
        }],
    })
    rejected = prompt_context.compile_prompt_context(
        sections=[{"prompt_id": "section", "start_frame": 0, "end_frame": 10,
                   "channels": {}, "attachments": [retention]}],
        window_start=0, window_end=10, template="minimax_h3_ref",
        context={
            "setup_manifest": {"setup": {"mode": "reference"},
                               "pictures": [{"member_id": "picture"}]},
            "ordinal_manifest": {"pictures": {"picture": 1}},
        })
    assert "unknown_declared_field_value" in {
        row["code"] for row in rejected["errors"]}
    assert not any(row["capability_id"] == "retention"
                   for row in rejected["emissions"])


def test_presentation_metadata_changes_hashes_without_changing_compiled_semantics():
    seed = prompt_context.profile_fork_seed(
        prompt_context.BUILTIN_PROFILES["generic@1"])
    left = {"profile_id": "same", "version": "1", "name": "Same", **seed}
    right = copy.deepcopy(left)
    right["capabilities"]["reference"]["derived"]["derived_prompt"][
        "help"] = "Different presentation-only guidance."
    capability = {"capability_id": "derived_prompt", "kind": "derived_prompt",
                  "placement": "inline", "enabled": True, "config": {}}
    left_result = _compile_reference_with(capability, profile=left)
    right_result = _compile_reference_with(capability, profile=right)
    for key in ("prompt", "channels", "segments", "emissions", "setup_manifest"):
        assert left_result[key] == right_result[key]
    assert left_result["profile_hash"] != right_result["profile_hash"]
    assert left_result["content_hash"] != right_result["content_hash"]


def test_profile_declarations_preserve_invalid_saved_data_but_refuse_resolution():
    raw = {
        "profile_id": "broken", "version": "1", "name": "Broken",
        "template_id": "standard", "capabilities": {}, "writing_aids": [],
        "physical_populations": "saved malformed declaration",
    }
    normalized = prompt_context.normalize_profile(raw)
    assert normalized["physical_populations"] == "saved malformed declaration"
    assert prompt_context.profile_declaration_errors(normalized)[0]["code"] == (
        "unsupported_population")
    with pytest.raises(prompt_context.ProfileResolutionError) as exc:
        prompt_context.resolve_profile(normalized, template="standard")
    assert exc.value.code == "invalid_profile_declaration"


def test_contribution_catalog_distinguishes_inherited_default_from_explicit_opt_out():
    inherited = {"physical_populations": []}
    opted_out = {"physical_populations": [], "contribution_catalog": {}}
    assert prompt_context.effective_contribution_catalog(inherited) == (
        prompt_context.DEFAULT_CONTRIBUTION_CATALOG)
    assert prompt_context.effective_contribution_catalog(opted_out) == {}


def test_prompt_compiler_has_no_minimax_template_id_equality_branch():
    source = (Path(prompt_context.__file__).read_text(encoding="utf-8"))
    assert re.search(
        r"template_id\s*==\s*['\"]minimax_h3", source) is None


def test_custom_profile_identity_is_separate_and_hash_is_server_owned():
    project = TimelineProject(project_id="project")
    seed = prompt_context.profile_fork_seed(
        prompt_context.BUILTIN_PROFILES["generic@1"])
    [created] = routes._create_prompt_context_profile(project, {
        "profile_id": "my_format", "version": "1", "name": "My Format",
        "definition": seed,
    })
    assert created["profile_id"] == "my_format"
    assert created["version"] == "1"
    assert created["name"] == "My Format"
    assert created["content_hash"]
    assert created["content_hash"] != "forged"

    forged = copy.deepcopy(seed)
    forged["content_hash"] = "forged"
    with pytest.raises(routes.ProjectMutationRequestError) as exc:
        routes._create_prompt_context_profile(project, {
            "profile_id": "bad", "version": "1", "name": "Bad",
            "definition": forged,
        })
    assert exc.value.code == "reserved_prompt_context_profile_field"


@pytest.mark.parametrize(("population", "retired_alias"), [
    ("videos", "edit"),
    ("videos", "continue"),
    ("standalone_audios", "reuse"),
    ("standalone_audios", "reference"),
])
def test_retired_role_aliases_remain_visible_but_block_compilation(
        population, retired_alias):
    manifest_key = population
    compiled = prompt_context.compile_prompt_context(
        sections=[], window_start=0, window_end=10,
        template="minimax_h3_ref",
        context={"setup_manifest": {
            "setup": {"mode": "reference"},
            manifest_key: [{"member_id": "saved", "role": retired_alias}],
        }})
    assert any(error["code"] == "unsupported_reference_role"
               and error["member_id"] == "saved"
               for error in compiled["errors"])
    assert retired_alias not in {
        row["value"] for row in
        prompt_context.MINIMAX_H3_ROLE_CATALOGS[population]}


def test_atomic_population_reuses_blank_lane_then_is_semantically_idempotent():
    scene = _blank_scene()
    first = _ensure(scene, "picture")
    state = scene.to_dict()
    second = _ensure(scene, "picture")
    assert first["lane_index"] == 0 and first["materialized"] is True
    assert second["lane_id"] == first["lane_id"]
    assert second["changed"] is False
    assert scene.to_dict() == state


@pytest.mark.parametrize("population,setup_key,physical", [
    ("picture", "picture_lane_ids", "pictures"),
    ("video", "video_lane_ids", "videos"),
    ("audio", "audio_lane_ids", "standalone_audios"),
])
def test_atomic_population_materializes_all_canonical_populations(
        population, setup_key, physical):
    scene = _blank_scene()
    result = _ensure(scene, population)
    setup = minimax_h3.active_setup(scene)
    recipe = scene.reference_lane_recipes[result["lane_index"]]
    assert setup["mode"] == "reference"
    assert setup[setup_key] == [result["lane_id"]]
    assert recipe.recipe["soft"]["physical_population"] == physical


def test_atomic_population_never_rewrites_custom_or_configured_lane():
    custom = ReferenceLaneRecipe(
        lane_id="custom", media_kind="video", recipe_id="custom:owned",
        recipe={"id": "custom:owned", "name": "Authored",
                "hard": {"assembly": "batch"}, "soft": {}})
    scene = Scene(
        scene_id="scene", reference_lane_count=1,
        reference_lane_configs=[LaneConfig(name="Keep", locked=True)],
        reference_lane_recipes=[custom])
    before_recipe = scene.reference_lane_recipes[0].to_dict()
    before_config = scene.reference_lane_configs[0].to_dict()
    result = _ensure(scene, "picture")
    assert result["lane_index"] == 1
    assert scene.reference_lane_recipes[0].to_dict() == before_recipe
    assert scene.reference_lane_configs[0].to_dict() == before_config


def test_exact_lane_owned_by_another_setup_is_not_reused():
    preset = routes._h3_population_preset("pictures")
    owned = routes._canonical_h3_lane_recipe("owned", preset)
    first = minimax_h3.default_reference_setup(picture_lane_ids=["owned"])
    first["setup_id"] = "first"
    active = minimax_h3.default_reference_setup()
    active["setup_id"] = "active"
    scene = Scene(
        scene_id="scene", reference_lane_count=1,
        reference_lane_configs=[LaneConfig(name="Owned")],
        reference_lane_recipes=[owned],
        minimax_h3_conditioning_setups=[first, active],
        active_minimax_h3_setup_id="active")
    result = _ensure(scene, "picture")
    assert result["lane_index"] == 1
    assert minimax_h3.active_setup(scene)["picture_lane_ids"] == [result["lane_id"]]
    assert scene.minimax_h3_conditioning_setups[0]["picture_lane_ids"] == ["owned"]


def test_atomic_population_operation_rejects_embedded_scene_or_extra_fields():
    with pytest.raises(routes.ProjectMutationRequestError) as exc:
        routes._ensure_minimax_h3_reference_population(_blank_scene(), {
            "type": "ensure_minimax_h3_reference_population",
            "population": "picture", "scene_id": "not-body-owned",
        })
    assert exc.value.code == "invalid_h3_reference_population_operation"


def test_stale_scene_mutation_precondition_rolls_back_before_h3_operation():
    scene = _blank_scene()
    project = TimelineProject(project_id="project", scenes=[scene],
                              modified_at="current")
    before = scene.to_dict()
    request = SimpleNamespace(method="POST", headers={"If-Match": "stale"})
    with pytest.raises(ProjectVersionConflict):
        routes._validate_request_project_version(request, project)
        _ensure(scene, "picture")
    assert scene.to_dict() == before
