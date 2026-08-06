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
from server.project_manager import load_project, save_project
from server.timeline_state import Asset, ReferenceEntity, ReferenceMember, TimelineProject
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
        "fields": {"name": "Character 1", "kind": "character", "reference_class": "subject", "description": ""},
    }])
    reference_id = payload["results"][0]["reference_id"]
    payload = routes._apply_reference_mutation_operations(project, [{
        "type": "create_member",
        "reference_id": reference_id,
        "fields": _member_fields(),
    }, {
        "type": "create_member",
        "reference_id": reference_id,
        "fields": _member_fields(tags=["Second"]),
    }])
    reference = project.references[0]
    assert reference.members[0].tags == ["sonder:portrait", "Sad"]
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
