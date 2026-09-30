import asyncio
import copy
import importlib
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server import prompt_context, timeline_state
from server.project_manager import load_project, save_project
from server.timeline_state import (
    ALL_REFERENCE_RECIPE_PRESETS, REFERENCE_TAG_FAMILIES,
    REFERENCE_TAG_PRESETS, Asset, LaneConfig, ReferenceEntity, ReferenceItem,
    ReferenceLaneRecipe, ReferenceMember, Scene, TimelineProject,
)
import server.routes as routes


class DummyRequest(dict):
    def __init__(self, *, match_info=None, body=None, method="GET", path="/", headers=None):
        super().__init__()
        self.match_info = match_info or {}
        self.query = {}
        self.headers = headers or {}
        self._body = body
        self.method = method
        self.path = path

    async def json(self):
        return self._body


def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef(), app=web.Application()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


def _project(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    project = TimelineProject(project_dir=str(project_dir), project_id="reference-project", name="References")
    project.assets = [
        Asset(asset_id="image-1", name="Portrait", asset_type="image", path="media/portrait.png"),
        Asset(asset_id="audio-1", name="Voice", asset_type="audio", path="media/voice.wav"),
        Asset(asset_id="video-1", name="Video", asset_type="video", path="media/video.mp4"),
        Asset(asset_id="video-audio", name="Performance", asset_type="video", path="media/performance.mp4", has_audio=True),
    ]
    return project


def _member_fields(asset_id="image-1", **overrides):
    fields = {
        "asset_id": asset_id,
        "tags": ["sonder:portrait", "Sad", " sad  "],
        "prompt": "sad portrait",
        "crop": {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8},
        "source_start_sec": 0,
        "source_end_sec": None,
    }
    fields.update(overrides)
    return fields


def test_references_do_not_auto_mint_prompt_identities(tmp_path):
    project = _project(tmp_path)
    project.references = [
        ReferenceEntity(reference_id="subject-ref", name="Lead",
                        reference_class="subject", members=[
                            ReferenceMember(member_id="portrait", asset_id="image-1")]),
        ReferenceEntity(reference_id="context-ref", name="Apartment",
                        reference_class="context"),
    ]
    loaded = TimelineProject.from_dict(project.to_dict(), str(tmp_path / "loaded"))
    assert loaded.prompt_semantic_units == []


def test_unreleased_source_members_convert_once_without_losing_contributions(tmp_path):
    project = _project(tmp_path)
    raw = project.to_dict()
    raw["prompt_semantic_units"] = [{
        "semantic_unit_id": "identity-1", "name": "Lead", "kind": "subject",
        "source_members": [{"entity_id": "ref-1", "member_id": "member-1"}],
        "definition": "a weathered traveler",
    }]
    loaded = TimelineProject.from_dict(raw, str(tmp_path / "loaded"))
    assert loaded.prompt_semantic_units[0]["sources"] == [{
        "entity_id": "ref-1", "member_id": "member-1",
        "contribution": "",
    }]
    assert "source_members" not in loaded.prompt_semantic_units[0]

    reloaded = TimelineProject.from_dict(loaded.to_dict(), str(tmp_path / "reloaded"))
    assert reloaded.prompt_semantic_units == loaded.prompt_semantic_units


def test_duplicate_explicit_identity_ids_are_repaired_without_merging(tmp_path):
    raw = _project(tmp_path).to_dict()
    raw["prompt_semantic_units"] = [
        {"semantic_unit_id": "duplicate", "name": "First", "definition": "one"},
        {"semantic_unit_id": "duplicate", "name": "Second", "definition": "two"},
    ]
    loaded = TimelineProject.from_dict(raw, str(tmp_path / "loaded"))
    assert [unit["name"] for unit in loaded.prompt_semantic_units] == ["First", "Second"]
    assert [unit["definition"] for unit in loaded.prompt_semantic_units] == ["one", "two"]
    assert loaded.prompt_semantic_units[0]["semantic_unit_id"] == "duplicate"
    assert loaded.prompt_semantic_units[1]["semantic_unit_id"] != "duplicate"


def test_reference_payload_has_no_generated_identity_projection(tmp_path):
    project = _project(tmp_path)
    project.prompt_semantic_units.append(prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "identity-1", "name": "Pair", "generated": True,
    }))

    payload = routes._references_payload(project)

    assert [value["semantic_unit_id"]
            for value in payload["prompt_semantic_units"]] == ["identity-1"]
    assert all("generated" not in value for value in payload["prompt_semantic_units"])
    assert all("generated" not in value for value in project.prompt_semantic_units)
    assert "generated" not in prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "unit:imported", "name": "Imported",
        "generated": True,
    })


def test_custom_recipe_context_catalog_rejects_unknown_values(tmp_path):
    project = _project(tmp_path)
    fields = {
        "name": "Unknown context",
        "media_kind": "image",
        "hard": {},
        "soft": {
            "compatible_profiles": ["missing@1"],
            "physical_population": "none",
            "exposed_capabilities": ["derived_prompt"],
            "role_fields": [],
        },
    }
    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "create_recipe", "fields": fields,
        }])
    assert (exc_info.value.status, exc_info.value.code) == (
        409, "unsupported_reference_recipe_context")


def test_recipe_capability_vocabulary_is_the_union_of_compatible_profiles(tmp_path):
    project = _project(tmp_path)
    project.prompt_context_profiles = [{
        "profile_id": "format_a", "version": "1",
        "capabilities": {"reference": {"derived": {
            "definitions": {"order": 1, "channel_key": "visual",
                            "placement": "section_prefix"},
        }}},
    }, {
        "profile_id": "format_b", "version": "1",
        "capabilities": {"reference": {"derived": {
            "audio_relationship": {"order": 1, "channel_key": "speech",
                                   "placement": "channel_suffix"},
        }}},
    }, {
        "profile_id": "unrelated", "version": "1",
        "capabilities": {"reference": {"derived": {
            "summary": {"order": 1, "channel_key": "visual",
                        "placement": "inline"},
        }}},
    }]
    accepted = {
        "name": "Two-format recipe", "media_kind": "image", "hard": {},
        "soft": {
            "compatible_profiles": ["format_a@1", "format_b@1"],
            "physical_population": "none",
            "exposed_capabilities": ["definitions", "audio_relationship"],
            "role_fields": [],
        },
    }
    routes._apply_reference_mutation_operations(project, [{
        "type": "create_recipe", "fields": accepted,
    }])
    assert project.reference_recipes[-1]["soft"]["exposed_capabilities"] == [
        "definitions", "audio_relationship"]

    refused = {**accepted, "name": "Leaky recipe", "soft": {
        **accepted["soft"], "exposed_capabilities": ["summary"],
    }}
    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "create_recipe", "fields": refused,
        }])
    assert (exc_info.value.status, exc_info.value.code) == (
        409, "unsupported_reference_recipe_context")


def test_blank_legacy_member_suffix_is_optional_in_delete_expected(tmp_path):
    project = _project(tmp_path)
    member = ReferenceMember(member_id="member-1", asset_id="image-1", name="")
    project.references = [ReferenceEntity(
        reference_id="ref-1", name="Legacy", members=[member])]
    expected = member.to_dict()
    expected.pop("name")
    routes._apply_reference_mutation_operations(project, [{
        "type": "delete_member", "reference_id": "ref-1",
        "member_id": "member-1", "expected": expected,
    }])
    assert project.references[0].members == []


def test_reference_and_member_deletion_prune_sources_and_legacy_voice(tmp_path):
    project = _project(tmp_path)
    project.references = [ReferenceEntity(
        reference_id="ref-1", name="Lead", members=[
            ReferenceMember(member_id="portrait", asset_id="image-1"),
            ReferenceMember(member_id="voice", asset_id="audio-1"),
        ])]
    project.prompt_semantic_units = [prompt_context.normalize_semantic_unit({
        "semantic_unit_id": "lead", "name": "Lead", "definition": "a lead",
        "sources": [
            {"entity_id": "ref-1", "member_id": "portrait",
             "contribution": "appearance", "inherit_description": True},
            {"entity_id": "ref-1", "member_id": "voice",
             "contribution": "timbre", "inherit_description": False},
        ],
        "voice": {"member_id": "voice"},
    })]
    portrait = project.references[0].members[0]
    routes._apply_reference_mutation_operations(project, [{
        "type": "delete_member", "reference_id": "ref-1",
        "member_id": "portrait", "expected": portrait.to_dict(),
    }])
    unit = project.prompt_semantic_units[0]
    assert [source["member_id"] for source in unit["sources"]] == ["voice"]
    assert unit["definition"] == "a lead"

    reference = project.references[0]
    routes._apply_reference_mutation_operations(project, [{
        "type": "delete_reference", "reference_id": "ref-1",
        "expected": {
            "name": reference.name, "kind": reference.kind,
            "reference_class": reference.reference_class,
            "description": reference.description,
            "visual_intent": reference.visual_intent,
            "audio_intent": reference.audio_intent,
            "member_ids": ["voice"],
        },
    }])
    assert unit["sources"] == []
    assert "voice" not in unit
    assert unit["definition"] == "a lead"


def test_video_population_accepts_only_video_members(tmp_path):
    project = _project(tmp_path)
    project.references = [ReferenceEntity(
        reference_id="ref-1", name="Motion", members=[
            ReferenceMember(member_id="video-member", asset_id="video-1"),
            ReferenceMember(member_id="image-member", asset_id="image-1"),
        ])]
    assert routes._canonical_reference_member_refs(project, [
        {"entity_id": "ref-1", "member_id": "video-member"}], "video") == [
        {"entity_id": "ref-1", "member_id": "video-member"}]
    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._canonical_reference_member_refs(project, [
            {"entity_id": "ref-1", "member_id": "image-member"}], "video")
    assert exc_info.value.code == "reference_media_kind_mismatch"


def test_reference_project_roundtrip_and_legacy_default(tmp_path):
    project = _project(tmp_path)
    project.references = [ReferenceEntity(
        reference_id="ref-1",
        name="Character 1",
        kind="character",
        reference_class="subject",
        description="Lead",
        members=[ReferenceMember(
            member_id="member-1",
            asset_id="image-1",
            name="Front view",
            tags=["sonder:portrait", "Sad"],
            prompt="portrait",
            crop={"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
        )],
    )]
    restored = TimelineProject.from_dict(project.to_dict(), project_dir=project.project_dir)
    assert restored.references[0].to_dict() == project.references[0].to_dict()
    assert TimelineProject.from_dict({"project_id": "legacy"}).references == []


def test_tolerant_load_is_stable_and_densifies_order():
    data = {
        "project_id": "stable-project",
        "references": [
            {
                "reference_id": "",
                "name": " ",
                "kind": "bad",
                "reference_class": "bad",
                "members": [
                    {"member_id": "duplicate", "asset_id": "missing", "order": 4},
                    {"member_id": "duplicate", "asset_id": "missing", "order": 1},
                    {
                        "member_id": "",
                        "asset_id": "missing",
                        "order": "bad",
                        "crop": {"x": -1, "y": 0, "w": 1, "h": 1},
                        "source_start_sec": -1,
                        "source_end_sec": 0,
                    },
                ],
            }
        ],
    }
    first = TimelineProject.from_dict(data)
    second = TimelineProject.from_dict(data)
    assert first.to_dict()["references"] == second.to_dict()["references"]
    reference = first.references[0]
    assert reference.name == "Untitled Reference"
    assert (reference.kind, reference.reference_class) == ("character", "subject")
    assert [member.order for member in reference.members] == [0, 1, 2]
    assert len({member.member_id for member in reference.members}) == 3
    assert reference.members[1].member_id == "duplicate"
    assert reference.members[-1].crop is None
    assert (reference.members[-1].source_start_sec, reference.members[-1].source_end_sec) == (0.0, None)

    invalid_tags = TimelineProject.from_dict({
        "project_id": "invalid-tags",
        "references": [{"members": [{"tags": ["kept?", 7]}]}],
    })
    assert invalid_tags.references[0].members[0].tags == []


def test_reference_mutation_crud_tags_and_reorder(tmp_path):
    project = _project(tmp_path)
    payload = routes._apply_reference_mutation_operations(project, [{
        "type": "create_reference",
        "fields": {"name": "Character 1", "kind": "character", "reference_class": "subject", "description": "",
                   "visual_intent": "preserve", "audio_intent": "reference_characteristics"},
    }])
    reference_id = payload["results"][0]["reference_id"]
    payload = routes._apply_reference_mutation_operations(project, [{
        "type": "create_member",
        "reference_id": reference_id,
        "fields": _member_fields(name="Front view"),
    }, {
        "type": "create_member",
        "reference_id": reference_id,
        "fields": _member_fields(tags=["Second"]),
    }])
    reference = project.references[0]
    assert reference.members[0].tags == ["sonder:portrait", "Sad"]
    assert reference.members[0].name == "Front view"
    assert (reference.visual_intent, reference.audio_intent) == (
        "preserve", "reference_characteristics")

    routes._apply_reference_mutation_operations(project, [{
        "type": "update_reference",
        "reference_id": reference_id,
        "fields": {"visual_intent": "partial", "audio_intent": "copy_partial"},
        "expected": {"visual_intent": "preserve",
                     "audio_intent": "reference_characteristics"},
    }])
    assert (reference.visual_intent, reference.audio_intent) == (
        "partial", "copy_partial")
    original = [member.member_id for member in reference.members]
    routes._apply_reference_mutation_operations(project, [{
        "type": "reorder_members",
        "reference_id": reference_id,
        "expected_member_ids": original,
        "member_ids": list(reversed(original)),
    }])
    assert [member.member_id for member in reference.members] == list(reversed(original))
    assert [member.order for member in reference.members] == [0, 1]

    canonical = routes._validated_reference_tags(["SONDER:PORTRAIT"], project.get_asset("image-1"))
    assert canonical == ["sonder:portrait"]


def test_reference_create_rejects_unknown_fields_and_accepts_finite_image_range(tmp_path):
    project = _project(tmp_path)
    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "create_reference",
            "fields": {"name": "Character", "kind": "character", "reference_class": "subject", "extra": True},
        }])
    assert exc_info.value.code == "invalid_reference_mutation"

    project.references = [ReferenceEntity(reference_id="ref-1", name="Character")]
    routes._apply_reference_mutation_operations(project, [{
        "type": "create_member",
        "reference_id": "ref-1",
        "fields": _member_fields(source_start_sec=1.0, source_end_sec=2.0),
    }])
    assert project.references[0].members[0].source_start_sec == 1.0


def test_image_reference_accepts_dormant_audio_default_and_rejects_bad_intent(tmp_path):
    project = _project(tmp_path)
    routes._apply_reference_mutation_operations(project, [{
        "type": "create_reference",
        "fields": {
            "name": "Still", "kind": "character", "reference_class": "subject",
            "description": "", "visual_intent": "preserve",
            "audio_intent": "copy_full",
        },
    }])
    assert project.references[0].audio_intent == "copy_full"
    with pytest.raises(routes.ProjectMutationRequestError) as invalid:
        routes._apply_reference_mutation_operations(project, [{
            "type": "update_reference", "reference_id": project.references[0].reference_id,
            "fields": {"audio_intent": "invented"},
            "expected": {"audio_intent": "copy_full"},
        }])
    assert invalid.value.code == "invalid_reference"


def test_reference_mutations_reject_stale_expected_values(tmp_path):
    project = _project(tmp_path)
    reference = ReferenceEntity(reference_id="ref-1", name="Current")
    member = ReferenceMember(member_id="member-1", asset_id="image-1", tags=["sonder:portrait"])
    reference.members = [member]
    project.references = [reference]

    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "update_reference",
            "reference_id": "ref-1",
            "fields": {"name": "Next"},
            "expected": {"name": "Stale"},
        }])
    assert (exc_info.value.status, exc_info.value.code) == (409, "identity_mismatch")

    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "update_member",
            "reference_id": "ref-1",
            "member_id": "member-1",
            "fields": {"prompt": "Next"},
            "expected": {"prompt": "Stale"},
        }])
    assert (exc_info.value.status, exc_info.value.code) == (409, "identity_mismatch")

    stale_member = member.to_dict()
    stale_member["tags"] = ["stale"]
    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "delete_member",
            "reference_id": "ref-1",
            "member_id": "member-1",
            "expected": stale_member,
        }])
    assert (exc_info.value.status, exc_info.value.code) == (409, "identity_mismatch")

    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "delete_reference",
            "reference_id": "ref-1",
            "expected": {
                "name": "Current", "kind": "character", "reference_class": "subject",
                "description": "", "member_ids": [],
            },
        }])
    assert (exc_info.value.status, exc_info.value.code) == (409, "identity_mismatch")

    # A delete snapshot that omits `description` is incomplete, not merely
    # stale: model-facing text is part of the exact-prior-value contract, so a
    # client that has not seen the field is refused before any comparison.
    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "delete_reference",
            "reference_id": "ref-1",
            "expected": {
                "name": "Stale", "kind": "character", "reference_class": "subject",
                "member_ids": [],
            },
        }])
    assert (exc_info.value.status, exc_info.value.code) == (400, "missing_expected_identity")

    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "reorder_members",
            "reference_id": "ref-1",
            "expected_member_ids": [],
            "member_ids": ["member-1"],
        }])
    assert (exc_info.value.status, exc_info.value.code) == (409, "identity_mismatch")


def test_reference_mutation_batch_is_atomic_until_save(tmp_path):
    project = _project(tmp_path)
    save_project(project)
    before = (tmp_path / "project" / "project.json").read_text(encoding="utf-8")
    loaded = load_project(project.project_dir)
    with pytest.raises(routes.ProjectMutationRequestError):
        routes._apply_reference_mutation_operations(loaded, [
            {"type": "create_reference", "fields": {"name": "Created", "kind": "character", "reference_class": "subject"}},
            {"type": "unsupported"},
        ])
    after = (tmp_path / "project" / "project.json").read_text(encoding="utf-8")
    assert before == after


@pytest.mark.parametrize("fields,code", [
    (_member_fields(tags=["sonder:voice_identity"]), "reference_tag_asset_mismatch"),
    (_member_fields(asset_id="audio-1", tags=["sonder:portrait"], crop=None), "reference_tag_asset_mismatch"),
    (_member_fields(asset_id="video-1", tags=["sonder:voice_identity"], crop=None), "reference_tag_asset_mismatch"),
    (_member_fields(tags=["sonder:future"]), "invalid_reference_tag"),
    (_member_fields(crop={"x": 0.5, "y": 0, "w": 0.6, "h": 1}), "invalid_reference_crop"),
])
def test_reference_member_write_validation(tmp_path, fields, code):
    project = _project(tmp_path)
    project.references = [ReferenceEntity(reference_id="ref-1", name="Character")]
    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(project, [{
            "type": "create_member",
            "reference_id": "ref-1",
            "fields": fields,
        }])
    assert exc_info.value.code == code


def test_video_reference_members_support_crop_trim_and_audio_capabilities(tmp_path):
    project = _project(tmp_path)
    project.references = [ReferenceEntity(reference_id="ref-1", name="Character")]
    routes._apply_reference_mutation_operations(project, [{
        "type": "create_member",
        "reference_id": "ref-1",
        "fields": _member_fields(
            asset_id="video-1",
            tags=["sonder:subject_clip", "sonder:motion_reference"],
            crop={"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7},
            source_start_sec=1.25,
            source_end_sec=3.5,
        ),
    }, {
        "type": "create_member",
        "reference_id": "ref-1",
        "fields": _member_fields(
            asset_id="video-audio",
            tags=["sonder:portrait", "sonder:voice_identity"],
            crop={"x": 0, "y": 0, "w": 1, "h": 1},
            source_start_sec=0.5,
            source_end_sec=None,
        ),
    }])
    visual, performance = project.references[0].members
    assert visual.asset_id == "video-1"
    assert visual.crop == {"x": 0.2, "y": 0.1, "w": 0.6, "h": 0.7}
    assert (visual.source_start_sec, visual.source_end_sec) == (1.25, 3.5)
    assert performance.tags == ["sonder:portrait", "sonder:voice_identity"]


def test_reference_usage_and_explicit_cleanup(tmp_path):
    project = _project(tmp_path)
    project.references = [ReferenceEntity(
        reference_id="ref-1",
        name="Character",
        members=[
            ReferenceMember(member_id="member-1", asset_id="image-1", order=0),
            ReferenceMember(member_id="member-2", asset_id="audio-1", order=1),
        ],
    )]
    usage = routes._find_asset_usages(project, project.get_asset("image-1"))
    assert usage["usage_count"] == 1
    assert usage["usages"][0]["type"] == "reference_member"
    cleanup = routes._remove_reference_members_for_assets(project, {"image-1"})
    assert cleanup["reference_members_removed"] == 1
    assert [member.member_id for member in project.references[0].members] == ["member-2"]
    assert project.references[0].members[0].order == 0


def test_single_bulk_and_empty_trash_prune_reference_edges(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = _project(tmp_path)
    (tmp_path / "project" / "media" / "portrait.png").write_bytes(b"image")
    (tmp_path / "project" / "media" / "voice.wav").write_bytes(b"audio")
    project.references = [ReferenceEntity(
        reference_id="ref-1",
        name="Character",
        members=[
            ReferenceMember(member_id="member-1", asset_id="image-1", order=0),
            ReferenceMember(member_id="member-2", asset_id="audio-1", order=1),
        ],
    )]
    save_project(project)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))

    single = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/permanent")
    response = asyncio.run(single(DummyRequest(
        match_info={"project_id": "project"}, method="POST",
        body={"asset_id": "image-1", "force": True},
    )))
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["reference_members_removed"] == 1
    assert payload["affected_reference_ids"] == ["ref-1"]
    restored = load_project(project.project_dir)
    assert [member.member_id for member in restored.references[0].members] == ["member-2"]
    assert restored.references[0].members[0].order == 0

    restored.get_asset("audio-1").trashed_at = "2026-08-01T00:00:00"
    save_project(restored)
    empty = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/empty-trash")
    response = asyncio.run(empty(DummyRequest(match_info={"project_id": "project"}, method="POST")))
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["reference_members_removed"] == 1
    assert load_project(project.project_dir).references[0].members == []

    restored = load_project(project.project_dir)
    (tmp_path / "project" / "media" / "alternate.png").write_bytes(b"image")
    restored.assets.append(Asset(asset_id="image-2", name="Alternate", asset_type="image", path="media/alternate.png"))
    restored.references[0].members = [ReferenceMember(member_id="member-3", asset_id="image-2", order=0)]
    save_project(restored)

    bulk = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/bulk-permanent-delete")
    response = asyncio.run(bulk(DummyRequest(
        match_info={"project_id": "project"}, method="POST",
        body={"asset_ids": ["image-2"], "force": True},
    )))
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["reference_members_removed"] == 1
    assert payload["affected_reference_ids"] == ["ref-1"]
    assert load_project(project.project_dir).references[0].members == []


def test_automatic_asset_purge_preserves_unresolved_member(tmp_path):
    project = _project(tmp_path)
    image = project.get_asset("image-1")
    image.trashed_at = "2000-01-01T00:00:00"
    project.references = [ReferenceEntity(
        reference_id="ref-1",
        name="Character",
        members=[ReferenceMember(member_id="member-1", asset_id="image-1")],
    )]
    assert routes._purge_expired_trashed_assets(project, retention_days=0) is True
    assert project.get_asset("image-1") is None
    assert project.references[0].members[0].asset_id == "image-1"


def test_read_only_load_does_not_persist_deterministic_repairs(tmp_path):
    project = _project(tmp_path)
    save_project(project)
    project_path = tmp_path / "project" / "project.json"
    data = json.loads(project_path.read_text(encoding="utf-8"))
    data["references"] = [{"reference_id": "", "name": "Character", "members": [{"member_id": ""}]}]
    project_path.write_text(json.dumps(data), encoding="utf-8")
    before_stat = project_path.stat()
    first = load_project(project.project_dir).references[0].to_dict()
    time.sleep(0.01)
    second = load_project(project.project_dir).references[0].to_dict()
    after_stat = project_path.stat()
    assert first == second
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns


def test_reference_routes_register_get_before_mutation_without_wildcard(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    paths = [(route.method, route.path) for route in route_module.routes]
    get_route = ("GET", "/sonder-editor/project/{project_id}/references")
    mutation_route = ("POST", "/sonder-editor/project/{project_id}/references/mutations")
    assert get_route in paths
    assert mutation_route in paths
    assert paths.index(get_route) < paths.index(mutation_route)
    assert not any("references/{" in path for _, path in paths)


def test_reference_get_is_read_only_and_returns_catalog(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = _project(tmp_path)
    save_project(project)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    path = tmp_path / "project" / "project.json"
    before = path.stat().st_mtime_ns
    handler = _route_handler(route_module, "GET", "/sonder-editor/project/{project_id}/references")
    response = asyncio.run(handler(DummyRequest(match_info={"project_id": "project"})))
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status == 200
    assert payload["references"] == []
    assert payload["tag_presets"] == routes._reference_catalog_payload()
    assert payload["tag_families"] == REFERENCE_TAG_FAMILIES
    assert len(payload["tag_presets"]) == 26
    assert not any(entry["id"].startswith("sonder:h3_")
                   for entry in payload["tag_presets"])
    assert all("family" in entry for entry in payload["tag_presets"])
    assert all(entry["family"] is None or entry["family"] in payload["tag_families"]
               for entry in payload["tag_presets"])
    assert all("suggested_kinds" in entry for entry in payload["tag_presets"])
    assert all(isinstance(entry["requires_audio"], bool) for entry in payload["tag_presets"])
    voice = next(entry for entry in payload["tag_presets"] if entry["id"] == "sonder:voice_identity")
    assert voice["requires_audio"] is True
    assert voice["asset_types"] == ["audio", "video"]
    assert path.stat().st_mtime_ns == before


def test_builtin_recipe_tag_advisories_resolve_to_live_catalog():
    tag_ids = {str(entry["id"]) for entry in REFERENCE_TAG_PRESETS}
    for recipe in ALL_REFERENCE_RECIPE_PRESETS:
        soft = recipe.get("soft", {})
        for key in ("suggested_tags",):
            for tag in soft.get(key, []) or []:
                assert tag in tag_ids, f"{recipe['id']} has unknown {key} entry {tag}"
        context_tag = soft.get("context_tag")
        if context_tag:
            assert context_tag in tag_ids, (
                f"{recipe['id']} has unknown context_tag {context_tag}")


def test_reference_mutation_rejects_stale_if_match(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = _project(tmp_path)
    save_project(project)
    base_version = project.modified_at
    current = load_project(project.project_dir)
    current.name = "Concurrent change"
    save_project(current)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    handler = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/references/mutations")
    request = DummyRequest(
        match_info={"project_id": "project"},
        method="POST",
        path="/sonder-editor/project/project/references/mutations",
        headers={"If-Match": base_version},
        body={"operations": [{
            "type": "create_reference",
            "fields": {"name": "Character", "kind": "character", "reference_class": "subject"},
        }]},
    )
    response = asyncio.run(route_module._project_error_middleware(request, handler))
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status == 409
    assert payload["code"] == "project_version_conflict"


def test_reference_mutation_is_covered_by_origin_guard(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    called = []

    async def handler(_request):
        called.append(True)
        return web.json_response({"status": "ok"})

    request = DummyRequest(
        method="POST",
        path="/sonder-editor/project/project/references/mutations",
        headers={"Host": "127.0.0.1:7822", "Origin": "https://example.invalid"},
    )
    response = asyncio.run(route_module._sonder_security_middleware(request, handler))
    payload = json.loads(response.body.decode("utf-8"))
    assert response.status == 403
    assert payload["code"] == "cross_origin_blocked"
    assert called == []


def test_member_attachment_defaults_round_trip_and_refuse_uninheritable_keys(tmp_path):
    """A physical member carries the same defaults bag an identity does.

    Without it a physical Reference could only supply prompt text and the two
    intents, so every other field had to be retyped on every chip.
    """
    project = _project(tmp_path)
    routes._apply_reference_mutation_operations(project, [{
        "type": "create_reference",
        "fields": {"name": "Still", "kind": "character",
                   "reference_class": "subject", "description": "",
                   "visual_intent": "preserve",
                   "audio_intent": "reference_characteristics"},
    }])
    reference_id = project.references[0].reference_id
    routes._apply_reference_mutation_operations(project, [{
        "type": "create_member", "reference_id": reference_id,
        "fields": _member_fields(attachment_defaults={
            "retention_detail": "She keeps her facial features.",
            "task_types": ["reference_generation"],
        }),
    }])
    member = project.references[0].members[0]
    assert member.attachment_defaults == {
        "retention_detail": "She keeps her facial features.",
        "task_types": ["reference_generation"],
    }

    # Survives a real save/load cycle.
    save_project(project)
    reloaded = load_project(str(project.project_dir))
    assert reloaded.references[0].members[0].attachment_defaults == (
        member.attachment_defaults)

    # An update carrying exact prior values replaces the bag.
    routes._apply_reference_mutation_operations(project, [{
        "type": "update_member", "reference_id": reference_id,
        "member_id": member.member_id,
        "fields": {"attachment_defaults": {"summary": "A quiet corridor."}},
        "expected": {"attachment_defaults": {
            "retention_detail": "She keeps her facial features.",
            "task_types": ["reference_generation"],
        }},
    }])
    assert project.references[0].members[0].attachment_defaults == {
        "summary": "A quiet corridor."}

    # A key the identity resolver would never iterate is refused at the route
    # rather than stored and silently ignored forever.
    with pytest.raises(routes.ProjectMutationRequestError) as invalid:
        routes._apply_reference_mutation_operations(project, [{
            "type": "update_member", "reference_id": reference_id,
            "member_id": member.member_id,
            "fields": {"attachment_defaults": {"invented_field": "x"}},
            "expected": {"attachment_defaults": {"summary": "A quiet corridor."}},
        }])
    assert invalid.value.code == "invalid_reference_member"
    assert project.references[0].members[0].attachment_defaults == {
        "summary": "A quiet corridor."}


def test_member_attachment_defaults_preserve_keys_from_another_format():
    """Load must not filter to the currently resolved format's field set.

    One project can hold members authored under several Prompt Formats, so
    filtering here would delete authored defaults at rest on every load.
    """
    member = ReferenceMember.from_dict({
        "member_id": "m1", "asset_id": "image-1",
        "attachment_defaults": {"summary": "kept", "future_format_field": "kept"},
    })
    assert member.attachment_defaults == {
        "summary": "kept", "future_format_field": "kept"}
    assert member.to_dict()["attachment_defaults"] == member.attachment_defaults


def test_member_intents_outside_the_vocabulary_are_preserved_not_blanked():
    """Normalization preserves; the mutation route still refuses new ones.

    Blanking on load destroyed authored data with no message and made the
    route-side refusal unreachable for anything already stored.
    """
    member = ReferenceMember.from_dict({
        "member_id": "m1", "asset_id": "image-1",
        "visual_intent": "retired_alias", "audio_intent": "also_retired",
    })
    assert member.visual_intent == "retired_alias"
    assert member.audio_intent == "also_retired"
    assert member.to_dict()["visual_intent"] == "retired_alias"

    # A canonical value still loads unchanged.
    canonical = ReferenceMember.from_dict({
        "member_id": "m2", "asset_id": "image-1",
        "visual_intent": next(iter(prompt_context.VISUAL_INTENTS)),
    })
    assert canonical.visual_intent in prompt_context.VISUAL_INTENTS


# -- a Library delete's staged-item cascade, reported per scene --------------------
#
# The route thins removed members out of staged items and deletes items left
# empty in every scene; the editor paints the same cascade and checks it
# against the per-scene report on the delete's result (`cascade.scenes`).

def _staged_cascade_project(tmp_path):
    project = _project(tmp_path)
    project.assets.append(Asset(asset_id="image-2", name="Profile", asset_type="image",
                                path="media/profile.png"))
    project.references = [ReferenceEntity(reference_id="ref-1", name="Lead", members=[
        ReferenceMember(member_id="member-a", asset_id="image-1", order=0),
        ReferenceMember(member_id="member-b", asset_id="image-2", order=1),
    ])]

    def staged(*keys):
        return [{"entity_id": "ref-1", "member_id": f"member-{key}"} for key in keys]

    def scene(scene_id, rows):
        return Scene(scene_id=scene_id, duration_frames=100, reference_lane_count=1,
                     reference_lane_configs=[LaneConfig()],
                     reference_lane_recipes=[ReferenceLaneRecipe(lane_id=f"lane-{scene_id}",
                                                                 media_kind="image")],
                     reference_items=[ReferenceItem(reference_item_id=item_id, lane_index=0,
                                                    start_frame=start, end_frame=start + 10,
                                                    members=members)
                                      for item_id, start, members in rows])

    project.scenes = [
        scene("scene-1", [("solo", 0, staged("a")), ("pair", 20, staged("a", "b")),
                          ("other", 40, staged("b"))]),
        scene("scene-2", [("solo", 0, staged("a"))]),
        scene("scene-3", [("keep", 0, staged("b"))]),
    ]
    return project


def _staged_ids(project):
    return {scene.scene_id: {item.reference_item_id: [member["member_id"] for member in item.members]
                             for item in scene.reference_items}
            for scene in project.scenes}


def test_delete_member_thins_and_removes_staged_items_and_reports_each_scene(tmp_path):
    project = _staged_cascade_project(tmp_path)
    member = project.references[0].members[0]
    payload = routes._apply_reference_mutation_operations(project, [{
        "type": "delete_member", "reference_id": "ref-1", "member_id": "member-a",
        "expected": member.to_dict()}])
    assert _staged_ids(project) == {
        "scene-1": {"pair": ["member-b"], "other": ["member-b"]},
        "scene-2": {},
        "scene-3": {"keep": ["member-b"]},
    }
    [result] = payload["results"]
    assert result["type"] == "delete_member"
    assert result["reference_id"] == "ref-1" and result["member_id"] == "member-a"
    # Item ids are unique only within a scene, so the report is per scene and
    # names no scene the delete did not touch.
    assert result["cascade"]["scenes"] == {
        "scene-1": {"removed_reference_item_ids": ["solo"],
                    "thinned_reference_item_ids": ["pair"]},
        "scene-2": {"removed_reference_item_ids": ["solo"],
                    "thinned_reference_item_ids": []},
    }
    # The flat keys stay for older readers.
    assert result["cascade"]["affected_scene_ids"] == ["scene-1", "scene-2"]
    assert result["cascade"]["removed_reference_item_ids"] == ["solo", "solo"]
    assert result["cascade"]["thinned_reference_item_ids"] == ["pair"]


def test_delete_reference_removes_every_item_its_members_staged(tmp_path):
    project = _staged_cascade_project(tmp_path)
    payload = routes._apply_reference_mutation_operations(project, [{
        "type": "delete_reference", "reference_id": "ref-1",
        "expected": {"name": "Lead", "kind": "character", "reference_class": "subject",
                     "description": "", "member_ids": ["member-a", "member-b"]}}])
    assert _staged_ids(project) == {"scene-1": {}, "scene-2": {}, "scene-3": {}}
    [result] = payload["results"]
    assert result["cascade"]["scenes"] == {
        "scene-1": {"removed_reference_item_ids": ["solo", "pair", "other"],
                    "thinned_reference_item_ids": []},
        "scene-2": {"removed_reference_item_ids": ["solo"], "thinned_reference_item_ids": []},
        "scene-3": {"removed_reference_item_ids": ["keep"], "thinned_reference_item_ids": []},
    }


def test_a_delete_that_touches_no_staged_item_reports_no_scene(tmp_path):
    project = _staged_cascade_project(tmp_path)
    for scene in project.scenes:
        scene.reference_items = []
    member = project.references[0].members[1]
    payload = routes._apply_reference_mutation_operations(project, [{
        "type": "delete_member", "reference_id": "ref-1", "member_id": "member-b",
        "expected": member.to_dict()}])
    assert payload["results"][0]["cascade"]["scenes"] == {}


def test_member_update_results_name_their_reference(tmp_path):
    project = _staged_cascade_project(tmp_path)
    payload = routes._apply_reference_mutation_operations(project, [{
        "type": "update_member", "reference_id": "ref-1", "member_id": "member-b",
        "fields": {"prompt": "profile"}, "expected": {"prompt": ""}}])
    assert payload["results"] == [{"type": "update_member", "reference_id": "ref-1",
                                   "member_id": "member-b"}]


def test_a_permanent_asset_delete_reports_its_staged_cascade_per_scene(monkeypatch, tmp_path):
    route_module = _load_route_module(monkeypatch)
    project = _staged_cascade_project(tmp_path)
    (tmp_path / "project" / "media" / "portrait.png").write_bytes(b"image")
    (tmp_path / "project" / "media" / "profile.png").write_bytes(b"image")
    save_project(project)
    monkeypatch.setattr(route_module, "_get_base_dir", lambda: str(tmp_path))
    single = _route_handler(route_module, "POST", "/sonder-editor/project/{project_id}/assets/permanent")
    response = asyncio.run(single(DummyRequest(
        match_info={"project_id": "project"}, method="POST",
        body={"asset_id": "image-1", "force": True},
    )))
    payload = json.loads(response.body.decode("utf-8"))
    # The keys the gallery and editor already read are unchanged.
    for key in ("deleted", "asset_id", "usages_orphaned", "reference_members_removed",
                "affected_reference_ids", "removed_reference_members", "affected_scene_ids",
                "removed_reference_item_ids", "thinned_reference_item_ids"):
        assert key in payload, key
    assert payload["removed_reference_members"] == [
        {"reference_id": "ref-1", "member_id": "member-a", "asset_id": "image-1"}]
    assert payload["scenes"] == {
        "scene-1": {"removed_reference_item_ids": ["solo"],
                    "thinned_reference_item_ids": ["pair"]},
        "scene-2": {"removed_reference_item_ids": ["solo"],
                    "thinned_reference_item_ids": []},
    }
    assert _staged_ids(load_project(project.project_dir))["scene-1"] == {
        "pair": ["member-b"], "other": ["member-b"]}


# --- Client-minted Library ids (Library paint-first Phase 3) ---------------

def _library_error(project, operations):
    """The refusal of one batch. On a copy, as the route saves only a batch
    that succeeds: operations before the refused one are never stored."""
    with pytest.raises(routes.ProjectMutationRequestError) as exc_info:
        routes._apply_reference_mutation_operations(copy.deepcopy(project), operations)
    return exc_info.value.status, exc_info.value.code


def _create_reference_op(reference_id=None, name="Minted"):
    op = {"type": "create_reference", "fields": {"name": name, "kind": "character"}}
    if reference_id is not None:
        op["reference_id"] = reference_id
    return op


def _create_member_op(reference_id, member_id=None, **fields):
    op = {"type": "create_member", "reference_id": reference_id,
          "fields": _member_fields(**fields)}
    if member_id is not None:
        op["member_id"] = member_id
    return op


def test_library_creates_adopt_an_op_level_client_id(tmp_path):
    project = _project(tmp_path)
    payload = routes._apply_reference_mutation_operations(project, [
        _create_reference_op("a" * 32),
        _create_member_op("a" * 32, "b" * 32),
    ])
    assert payload["results"] == [
        {"type": "create_reference", "reference_id": "a" * 32},
        {"type": "create_member", "reference_id": "a" * 32, "member_id": "b" * 32},
    ]
    assert project.references[0].reference_id == "a" * 32
    assert project.references[0].members[0].member_id == "b" * 32


def test_an_absent_library_id_is_minted_by_the_server(tmp_path):
    project = _project(tmp_path)
    for absent in (None, ""):
        payload = routes._apply_reference_mutation_operations(project, [
            _create_reference_op(absent, name=f"R{absent!r}")])
        reference_id = payload["results"][0]["reference_id"]
        assert len(reference_id) == 32
        payload = routes._apply_reference_mutation_operations(project, [
            _create_member_op(reference_id, absent)])
        assert len(payload["results"][0]["member_id"]) == 32


@pytest.mark.parametrize("bad", ["Has Space", "ab", "UPPER-case", "-lead", "x" * 65, 12345, ["id"]])
def test_a_library_id_of_the_wrong_shape_or_type_is_refused(tmp_path, bad):
    project = _project(tmp_path)
    project.references = [ReferenceEntity(reference_id="ref-1", name="Held")]
    assert _library_error(project, [_create_reference_op(bad)]) == (400, "invalid_id")
    assert _library_error(project, [_create_member_op("ref-1", bad)]) == (400, "invalid_id")


def test_a_taken_library_id_is_refused_never_re_minted(tmp_path):
    project = _project(tmp_path)
    project.references = [
        ReferenceEntity(reference_id="ref-1", name="One", members=[
            ReferenceMember(member_id="member-1", asset_id="image-1")]),
        ReferenceEntity(reference_id="ref-2", name="Two"),
    ]
    assert _library_error(project, [_create_reference_op("ref-1")]) == (409, "id_conflict")
    # Member ids are unique project-wide: another Reference's member is taken.
    assert _library_error(project, [_create_member_op("ref-2", "member-1")]) == (409, "id_conflict")
    # Two creates in one batch minting the same id.
    assert _library_error(project, [
        _create_reference_op("fresh-id"), _create_reference_op("fresh-id")]) == (409, "id_conflict")


def test_an_id_a_delete_retired_earlier_in_the_batch_is_taken(tmp_path):
    """`[delete R, create R]` is refused, because the save would hand the new R
    the old R's unknown fields (`_preserve_project_unknown_fields`)."""
    project = _project(tmp_path)
    member = ReferenceMember(member_id="member-1", asset_id="image-1")
    project.references = [ReferenceEntity(reference_id="ref-1", name="Old", members=[member])]
    raw = project.to_dict()
    raw["references"][0]["future_field"] = "kept for R"
    raw["references"][0]["members"][0]["future_field"] = "kept for M"
    delete_reference = {"type": "delete_reference", "reference_id": "ref-1", "expected": {
        "name": "Old", "kind": "character", "reference_class": "subject", "description": "",
        "member_ids": ["member-1"]}}
    assert _library_error(project, [delete_reference, _create_reference_op("ref-1")]) == (
        409, "id_conflict")
    assert _library_error(project, [delete_reference, _create_reference_op("fresh-ref"),
                                    _create_member_op("fresh-ref", "member-1")]) == (409, "id_conflict")
    delete_member = {"type": "delete_member", "reference_id": "ref-1", "member_id": "member-1",
                     "expected": member.to_dict()}
    assert _library_error(project, [delete_member, _create_member_op("ref-1", "member-1")]) == (
        409, "id_conflict")

    # The reason: a row reusing the id inherits what the loaded document held.
    reused = TimelineProject.from_dict(raw, str(tmp_path / "reused"))
    reused.references = []
    routes._apply_create_reference(reused, {"name": "New"}, reference_id="ref-1")
    preserved = timeline_state._preserve_project_unknown_fields(raw, reused.to_dict())
    assert preserved["references"][0]["future_field"] == "kept for R"


def test_a_reference_id_in_fields_stays_an_unknown_field(tmp_path):
    """The id travels at operation level, which an older server ignores; in
    `fields` it would be refused there."""
    project = _project(tmp_path)
    op = {"type": "create_reference", "fields": {"name": "R", "kind": "character",
                                                  "reference_id": "a" * 32}}
    assert _library_error(project, [op]) == (400, "invalid_reference_mutation")


def _recipe_fields(**overrides):
    fields = {"name": "Custom", "media_kind": "image", "hard": {}, "soft": {}}
    fields.update(overrides)
    return fields


def test_create_recipe_refuses_a_taken_client_id_instead_of_re_minting(tmp_path):
    project = _project(tmp_path)
    project.reference_recipes = [{"id": "custom:held", "name": "Held", "builtIn": False,
                                  "media_kind": "image", "hard": {}, "soft": {}}]
    project.scenes = [Scene(scene_id="scene-1", reference_lane_count=1,
                            reference_lane_configs=[LaneConfig()],
                            reference_lane_recipes=[ReferenceLaneRecipe(
                                lane_id="lane-1", recipe_id="custom:dangling", media_kind="image")])]
    create = lambda recipe_id: {"type": "create_recipe", "fields": _recipe_fields(id=recipe_id)}
    assert _library_error(project, [create("custom:held")]) == (409, "id_conflict")
    # A lane still names it: reusing it would re-link that lane silently.
    assert _library_error(project, [create("custom:dangling")]) == (409, "id_conflict")
    delete = {"type": "delete_recipe", "recipe_id": "custom:held",
              "expected": {"id": "custom:held", "name": "Held", "media_kind": "image",
                           "hard": {}, "soft": {}}}
    assert _library_error(project, [delete, create("custom:held")]) == (409, "id_conflict")
    for bad in ("custom:Has Space", "custom:ab", "plain-id", 7):
        assert _library_error(project, [create(bad)]) == (400, "invalid_id")

    minted = "custom:" + "c" * 32
    payload = routes._apply_reference_mutation_operations(project, [create(minted)])
    assert payload["results"] == [{"type": "create_recipe", "recipe_id": minted}]
    # A server-minted id is still the server's to choose.
    payload = routes._apply_reference_mutation_operations(project, [
        {"type": "create_recipe", "fields": _recipe_fields(name="Server minted")}])
    assert payload["results"][0]["recipe_id"].startswith("custom:")


def test_a_stored_recipe_id_is_never_revalidated_by_an_update(tmp_path):
    project = _project(tmp_path)
    legacy = {"id": "My Legacy Recipe", "name": "Legacy", "builtIn": False,
              "media_kind": "image", "hard": {}, "soft": {}}
    project.reference_recipes = [dict(legacy)]
    expected = {key: legacy[key] for key in ("id", "name", "media_kind", "hard", "soft")}
    routes._apply_reference_mutation_operations(project, [{
        "type": "update_recipe", "recipe_id": "My Legacy Recipe",
        "fields": {"name": "Renamed"}, "expected": expected}])
    assert project.reference_recipes[0]["id"] == "My Legacy Recipe"
    assert project.reference_recipes[0]["name"] == "Renamed"


def test_the_library_payload_advertises_client_minted_ids(tmp_path):
    assert routes._references_payload(_project(tmp_path))["client_ids"] == [
        "reference", "member", "recipe"]
