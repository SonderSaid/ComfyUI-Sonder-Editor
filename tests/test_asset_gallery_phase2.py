"""Tests for asset gallery Phase 2 plan-aligned semantics."""

import asyncio
import importlib
import json
import os
import sys
from types import SimpleNamespace

from aiohttp import web
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import server.routes as routes
from server.timeline_state import Asset, AudioTrack, ClipReference, GenerationJob, GuideFrame, Scene, TimelineProject


class DummyRequest:
    def __init__(self, *, match_info=None, query=None, body=None):
        self.match_info = match_info or {}
        self.query = query or {}
        self._body = body
        self.content_length = 0 if body is None else len(json.dumps(body).encode("utf-8"))

    async def json(self):
        return self._body


def _make_project(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "media").mkdir(parents=True)
    (project_dir / "cache" / "thumbnails").mkdir(parents=True)
    (project_dir / "cache" / "waveforms").mkdir(parents=True)
    return TimelineProject(project_dir=str(project_dir), name="Phase 2")


def _write_project_file(project, relative_path, content=b"x"):
    absolute = os.path.join(project.project_dir, relative_path)
    os.makedirs(os.path.dirname(absolute), exist_ok=True)
    with open(absolute, "wb") as handle:
        handle.write(content)
    return absolute


def _write_asset_cache(project, asset_id):
    thumb = os.path.join(project.project_dir, "cache", "thumbnails", f"{asset_id}.png")
    strip = os.path.join(project.project_dir, "cache", "thumbnails", f"{asset_id}_strip.jpg")
    strip_info = strip + ".json"
    waveform = os.path.join(project.project_dir, "cache", "waveforms", f"{asset_id}.json")
    for path in [thumb, strip, strip_info, waveform]:
        with open(path, "wb") as handle:
            handle.write(b"cache")
    return [thumb, strip, strip_info, waveform]


def _response_json(response):
    return json.loads(response.body.decode("utf-8"))


def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def test_asset_payload_marks_missing_from_disk(tmp_path):
    project = _make_project(tmp_path)
    existing_path = tmp_path / "project" / "media" / "clip.mp4"
    existing_path.write_bytes(b"video")

    existing = Asset(asset_id="vid1", asset_type="video", path="media/clip.mp4")
    missing = Asset(asset_id="img1", asset_type="image", path="media/missing.png")

    assert routes._asset_payload(project, existing)["missing"] is False
    assert routes._asset_payload(project, missing)["missing"] is True


def test_find_asset_usages_returns_unified_usage_list(tmp_path):
    project = _make_project(tmp_path)
    asset = Asset(asset_id="asset-1", asset_type="video", path="media/clip.mp4", name="Clip")
    scene = Scene(scene_id="scene-1", name="Opening", order=1)
    scene.clips = [
        ClipReference(clip_id="clip-1", source_path="media/clip.mp4", timeline_start_frame=0, timeline_end_frame=24, track_index=2),
    ]
    scene.audio_tracks = [
        AudioTrack(track_id="audio-1", source_path="media/clip.mp4", timeline_start_frame=5, timeline_end_frame=30, lane_index=1),
    ]
    scene.guide_frames = [GuideFrame(frame_index=12, asset_id="asset-1")]
    project.scenes = [scene]
    project.generation_queue = [
        GenerationJob(job_id="job-1", scene_id="scene-1", scene_name="Opening", status="pending", result_asset_id="asset-1"),
    ]

    usage = routes._find_asset_usages(project, asset)

    assert usage["asset_id"] == "asset-1"
    assert usage["usage_count"] == 4
    assert [entry["type"] for entry in usage["usages"]] == [
        "clip",
        "audio_track",
        "guide_frame",
        "generation_job",
    ]
    assert usage["usages"][0]["clip_id"] == "clip-1"
    assert usage["usages"][1]["track_id"] == "audio-1"
    assert usage["usages"][2]["frame_index"] == 12
    assert usage["usages"][3]["job_id"] == "job-1"


def test_rename_project_asset_folder_updates_assets_and_returns_assets_moved(tmp_path):
    project = _make_project(tmp_path)
    project.assets = [
        Asset(asset_id="a1", folder="Shots"),
        Asset(asset_id="a2", folder="Shots/Cutaways"),
        Asset(asset_id="a3", folder="Other"),
    ]
    project.metadata["asset_folders"] = ["Shots", "Shots/Cutaways", "Other", "Unused"]

    folders, assets_moved = routes._rename_project_asset_folder(project, "Shots", "Footage")

    assert project.assets[0].folder == "Footage"
    assert project.assets[1].folder == "Footage/Cutaways"
    assert project.assets[2].folder == "Other"
    assert folders == ["Footage", "Footage/Cutaways", "Other", "Unused"]
    assert assets_moved == 2


def test_delete_project_asset_removes_registry_entry_and_leaves_references(tmp_path):
    project = _make_project(tmp_path)
    asset = Asset(asset_id="asset-1", asset_type="video", path="media/clip.mp4", name="Clip")
    scene = Scene(scene_id="scene-1", name="Opening", order=1)
    scene.clips = [ClipReference(clip_id="clip-1", source_path="media/clip.mp4", timeline_start_frame=0, timeline_end_frame=24)]
    scene.guide_frames = [GuideFrame(frame_index=12, asset_id="asset-1")]
    project.assets = [asset]
    project.scenes = [scene]

    source_path = _write_project_file(project, "media/clip.mp4", b"video")
    cache_paths = _write_asset_cache(project, asset.asset_id)

    payload = routes._delete_project_asset(project, asset, usages_orphaned=2)

    assert payload == {"deleted": True, "asset_id": "asset-1", "usages_orphaned": 2}
    assert project.get_asset("asset-1") is None
    assert scene.clips[0].source_path == "media/clip.mp4"
    assert scene.guide_frames[0].asset_id == "asset-1"
    assert not os.path.exists(source_path)
    assert all(not os.path.exists(path) for path in cache_paths)


def test_delete_project_asset_folder_removes_contained_assets_and_files(tmp_path):
    project = _make_project(tmp_path)
    project.assets = [
        Asset(asset_id="a1", folder="Shots", path="media/a1.mp4"),
        Asset(asset_id="a2", folder="Shots/Cutaways", path="media/a2.png", asset_type="image"),
        Asset(asset_id="a3", folder="Other", path="media/a3.wav", asset_type="audio"),
    ]
    project.metadata["asset_folders"] = ["Shots", "Shots/Cutaways", "Other"]

    removed_a1 = _write_project_file(project, "media/a1.mp4", b"a1")
    removed_a2 = _write_project_file(project, "media/a2.png", b"a2")
    kept_a3 = _write_project_file(project, "media/a3.wav", b"a3")
    _write_asset_cache(project, "a1")
    _write_asset_cache(project, "a2")
    kept_cache = _write_asset_cache(project, "a3")

    folders, deleted_assets = routes._delete_project_asset_folder(project, "Shots")

    assert folders == ["Other"]
    assert {asset.asset_id for asset in deleted_assets} == {"a1", "a2"}
    assert [asset.asset_id for asset in project.assets] == ["a3"]
    assert not os.path.exists(removed_a1)
    assert not os.path.exists(removed_a2)
    assert os.path.exists(kept_a3)
    assert all(os.path.exists(path) for path in kept_cache)


def test_replace_project_asset_updates_references_when_path_changes(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    media_dir = tmp_path / "project" / "media"
    old_path = media_dir / "clip.mp4"
    old_path.write_bytes(b"old")
    replacement_source = tmp_path / "replacement.mov"
    replacement_source.write_bytes(b"new")

    asset = Asset(asset_id="asset-1", asset_type="video", path="media/clip.mp4", name="clip.mp4")
    scene = Scene(scene_id="scene-1", name="Opening", order=1)
    scene.clips = [ClipReference(clip_id="clip-1", source_path="media/clip.mp4")]
    scene.audio_tracks = [AudioTrack(track_id="audio-1", source_path="media/clip.mp4")]
    project.assets = [asset]
    project.scenes = [scene]

    monkeypatch.setattr(
        "server.routes._extract_asset_media_metadata",
        lambda path, asset_type: {
            "width": 1280,
            "height": 720,
            "frame_count": 48,
            "fps": 24.0,
            "duration_sec": 2.0,
            "sample_rate": 0,
            "has_audio": True,
        },
    )

    updated = routes._replace_project_asset(project, asset, str(replacement_source))
    normalized_path = updated.path.replace("\\", "/")

    assert normalized_path.startswith("media/")
    assert normalized_path.endswith("_replacement.mov")
    assert updated.width == 1280
    assert updated.height == 720
    assert updated.frame_count == 48
    assert scene.clips[0].source_path == updated.path
    assert scene.audio_tracks[0].source_path == updated.path
    assert not old_path.exists()
    assert os.path.isfile(os.path.join(project.project_dir, updated.path))


def test_rename_project_asset_folder_rejects_conflicting_merge(tmp_path):
    project = _make_project(tmp_path)
    project.assets = [
        Asset(asset_id="a1", folder="Shots"),
        Asset(asset_id="a2", folder="Archive"),
    ]
    project.metadata["asset_folders"] = ["Shots", "Archive"]

    with pytest.raises(FileExistsError):
        routes._rename_project_asset_folder(project, "Shots", "Archive")


def test_delete_asset_route_returns_409_for_in_use_asset(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)
    asset = Asset(asset_id="asset-1", asset_type="video", path="media/clip.mp4", name="Clip")
    scene = Scene(scene_id="scene-1", name="Opening", order=1)
    scene.clips = [ClipReference(clip_id="clip-1", source_path="media/clip.mp4", timeline_start_frame=0, timeline_end_frame=24)]
    project.assets = [asset]
    project.scenes = [scene]

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "save_project", lambda project: None)

    request = DummyRequest(match_info={"project_id": "phase-2", "asset_id": "asset-1"}, body={"force": False})
    response = asyncio.run(module.api_delete_asset(request))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["error"] == "Asset is in use"
    assert payload["usage_count"] == 1
    assert payload["usages"][0]["type"] == "clip"
    assert project.get_asset("asset-1") is not None


def test_delete_asset_route_force_removes_asset_from_registry(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)
    asset = Asset(asset_id="asset-1", asset_type="video", path="media/clip.mp4", name="Clip")
    scene = Scene(scene_id="scene-1", name="Opening", order=1)
    scene.clips = [ClipReference(clip_id="clip-1", source_path="media/clip.mp4", timeline_start_frame=0, timeline_end_frame=24)]
    scene.guide_frames = [GuideFrame(frame_index=12, asset_id="asset-1")]
    project.assets = [asset]
    project.scenes = [scene]
    _write_project_file(project, "media/clip.mp4", b"video")
    _write_asset_cache(project, asset.asset_id)

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "save_project", lambda project: None)

    request = DummyRequest(match_info={"project_id": "phase-2", "asset_id": "asset-1"}, body={"force": True})
    response = asyncio.run(module.api_delete_asset(request))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["deleted"] is True
    assert payload["asset_id"] == "asset-1"
    assert payload["usages_orphaned"] == 2
    assert project.get_asset("asset-1") is None
    assert scene.clips[0].source_path == "media/clip.mp4"
    assert scene.guide_frames[0].asset_id == "asset-1"


def test_delete_asset_folder_route_returns_409_when_contained_assets_are_in_use(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)
    project.assets = [
        Asset(asset_id="a1", folder="Shots", path="media/a1.mp4"),
        Asset(asset_id="a2", folder="Shots/Cutaways", path="media/a2.png", asset_type="image"),
        Asset(asset_id="a3", folder="Other", path="media/a3.wav", asset_type="audio"),
    ]
    project.metadata["asset_folders"] = ["Shots", "Shots/Cutaways", "Other"]
    scene = Scene(scene_id="scene-1", name="Opening", order=1)
    scene.clips = [ClipReference(clip_id="clip-1", source_path="media/a1.mp4", timeline_start_frame=0, timeline_end_frame=24)]
    project.scenes = [scene]

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "save_project", lambda project: None)

    request = DummyRequest(match_info={"project_id": "phase-2"}, body={"folder": "Shots", "force": False})
    response = asyncio.run(module.api_delete_asset_folder(request))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["usage_count"] == 1
    assert payload["usages"][0]["asset_id"] == "a1"
    assert [asset.asset_id for asset in project.assets] == ["a1", "a2", "a3"]
