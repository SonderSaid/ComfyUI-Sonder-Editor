import asyncio
import importlib
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from aiohttp import web

import server
from server import prompt_context
from server.project_manager import load_project, save_project
from server.timeline_state import (
    Asset, ReferenceEntity, ReferenceMember, TimelineProject,
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
        "contribution": "", "inherit_description": False,
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


def test_reference_and_member_deletion_prune_identity_sources_and_voice(tmp_path):
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
    assert unit["voice"] == {"member_id": None}
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
    assert {entry["id"] for entry in payload["tag_presets"]} >= {
        "sonder:portrait", "sonder:voice_identity", "sonder:subject_clip",
        "sonder:context_clip", "sonder:motion_reference",
    }
    assert all("suggested_kinds" in entry for entry in payload["tag_presets"])
    assert all(isinstance(entry["requires_audio"], bool) for entry in payload["tag_presets"])
    voice = next(entry for entry in payload["tag_presets"] if entry["id"] == "sonder:voice_identity")
    assert voice["requires_audio"] is True
    assert voice["asset_types"] == ["audio", "video"]
    assert path.stat().st_mtime_ns == before


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
    response = asyncio.run(route_module._project_conflict_middleware(request, handler))
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
