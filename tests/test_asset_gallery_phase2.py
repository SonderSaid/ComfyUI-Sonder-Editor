"""Tests for asset gallery Phase 2 plan-aligned semantics."""

import asyncio
import importlib
import json
import os
import sys
from datetime import datetime, timedelta
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


def test_video_has_audio_uses_extraction_fallback(tmp_path, monkeypatch):
    video_path = tmp_path / "clip.mp4"
    video_path.write_bytes(b"video")

    class FailedProbe:
        returncode = 1
        stdout = ""
        stderr = ""

    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: FailedProbe())
    monkeypatch.setitem(sys.modules, "mutagen", SimpleNamespace(File=lambda filepath: None))
    monkeypatch.setitem(sys.modules, "torchaudio", SimpleNamespace(info=lambda filepath: (_ for _ in ()).throw(RuntimeError("probe failed"))))

    def fake_extract(video_path_arg, output_path):
        with open(output_path, "wb") as handle:
            handle.write(b"wav")
        return True

    monkeypatch.setattr(routes, "_extract_audio_from_video", fake_extract)

    assert routes._video_has_audio(str(video_path)) is True


def test_ffmpeg_stderr_summary_omits_banner():
    stderr = "\n".join([
        "ffmpeg version 4.2.2 Copyright (c) 2000-2019 the FFmpeg developers",
        "  built with gcc 9.2.1",
        "  configuration: --enable-gpl",
        "Output file #0 does not contain any stream",
    ])

    assert routes._summarize_ffmpeg_stderr(stderr) == "Output file #0 does not contain any stream"
    assert routes._ffmpeg_no_audio_stderr(stderr) is True


def test_sync_media_folder_repairs_false_video_has_audio_flags(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _write_project_file(project, "media/clip.mp4", b"video")
    project.assets = [
        Asset(asset_id="vid-1", asset_type="video", path="media/clip.mp4", has_audio=False),
    ]

    monkeypatch.setattr(routes, "_video_has_audio", lambda filepath: True)

    assert routes._sync_media_folder(project) is True
    assert project.assets[0].has_audio is True
    assert project.assets[0].has_audio_checked is True
    assert project.assets[0].media_probe_signature


def test_sync_media_folder_caches_no_audio_video_probe(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _write_project_file(project, "media/silent.mp4", b"video")
    project.assets = [
        Asset(asset_id="vid-1", asset_type="video", path="media/silent.mp4", has_audio=False),
    ]

    calls = []

    def fake_video_has_audio(filepath):
        calls.append(filepath)
        return False

    monkeypatch.setattr(routes, "_video_has_audio", fake_video_has_audio)

    assert routes._sync_media_folder(project) is True
    assert project.assets[0].has_audio is False
    assert project.assets[0].has_audio_checked is True
    assert project.assets[0].media_probe_signature
    assert routes._sync_media_folder(project) is False
    assert len(calls) == 1


def test_sync_media_folder_caches_failed_audio_duration_probe(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    _write_project_file(project, "media/no-duration.wav", b"audio")
    project.assets = [
        Asset(asset_id="aud-1", asset_type="audio", path="media/no-duration.wav", duration_sec=0),
    ]

    calls = []

    def fake_get_audio_duration(filepath):
        calls.append(filepath)
        return 0

    monkeypatch.setattr(routes, "_get_audio_duration", fake_get_audio_duration)

    assert routes._sync_media_folder(project) is True
    assert project.assets[0].duration_sec == 0
    assert project.assets[0].duration_checked is True
    assert project.assets[0].media_probe_signature
    assert routes._sync_media_folder(project) is False
    assert len(calls) == 1


def test_sync_media_folder_reprobes_when_media_signature_changes(tmp_path, monkeypatch):
    project = _make_project(tmp_path)
    media_path = _write_project_file(project, "media/changing.mp4", b"video")
    project.assets = [
        Asset(asset_id="vid-1", asset_type="video", path="media/changing.mp4", has_audio=False),
    ]

    calls = []

    def fake_video_has_audio(filepath):
        calls.append(routes._media_probe_signature(filepath))
        return len(calls) == 2

    monkeypatch.setattr(routes, "_video_has_audio", fake_video_has_audio)

    assert routes._sync_media_folder(project) is True
    first_signature = project.assets[0].media_probe_signature
    assert project.assets[0].has_audio is False

    with open(media_path, "wb") as handle:
        handle.write(b"video-with-new-audio-stream")

    assert routes._sync_media_folder(project) is True
    assert project.assets[0].has_audio is True
    assert project.assets[0].media_probe_signature != first_signature
    assert len(calls) == 2


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


def test_collect_asset_folders_excludes_trashed_assets(tmp_path):
    project = _make_project(tmp_path)
    project.assets = [
        Asset(asset_id="a1", folder="Shots"),
        Asset(asset_id="a2", folder="", trashed_at="2026-04-05T12:00:00", trash_previous_folder="Archive"),
    ]
    project.metadata["asset_folders"] = ["Shots"]

    assert routes._collect_asset_folders(project) == ["Shots"]


def test_list_dormant_assets_filters_trashed_by_default(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)
    project.assets = [
        Asset(asset_id="a1", path="media/a1.mp4", asset_type="video"),
        Asset(asset_id="a2", path="media/a2.png", asset_type="image", trashed_at="2026-04-05T12:00:00", trash_previous_folder="Archive"),
    ]

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)

    hidden_request = DummyRequest(match_info={"project_id": "phase-4"}, query={})
    hidden_response = asyncio.run(module.api_list_dormant_assets(hidden_request))
    hidden_payload = _response_json(hidden_response)

    shown_request = DummyRequest(match_info={"project_id": "phase-4"}, query={"include_trashed": "true"})
    shown_response = asyncio.run(module.api_list_dormant_assets(shown_request))
    shown_payload = _response_json(shown_response)

    assert [asset["asset_id"] for asset in hidden_payload["assets"]] == ["a1"]
    assert {asset["asset_id"] for asset in shown_payload["assets"]} == {"a1", "a2"}


def test_add_queue_job_route_persists_snapshot_fields(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "save_project", lambda project: None)

    request = DummyRequest(match_info={"project_id": "phase-4"}, body={
        "scene_id": "scene-1",
        "scene_name": "Opening",
        "selection_start": 24,
        "selection_end": 72,
        "batch_id": "batch-123",
        "batch_total": 3,
        "batch_index": 1,
        "prompt": "queued prompt",
        "context_frames": 12,
        "pre_context_frames": 8,
        "post_context_frames": 12,
        "mask_pre_offset": 2,
        "mask_post_offset": 3,
        "guide_frame_snapshots": [
            {"frame_index": 20, "asset_id": "guide-1", "source": "asset", "strength": 0.7, "muted": True},
        ],
        "prompt_sections": [
            {"start_frame": 0, "end_frame": 96, "prompt": "section prompt"},
        ],
        "scene_width": 1024,
        "scene_height": 576,
        "scene_fps": 30.0,
        "template_id": "ltxv-2.3",
        "frame_constraint": {"step": 8, "offset": 1, "min": 1, "max": 257},
        "take_placement_mode": "untrimmed",
    })
    response = asyncio.run(module.api_add_queue_job(request))
    payload = _response_json(response)

    assert response.status == 200
    assert len(project.generation_queue) == 1
    assert payload["batch_id"] == "batch-123"
    assert payload["batch_total"] == 3
    assert payload["batch_index"] == 1
    assert payload["pre_context_frames"] == 8
    assert payload["post_context_frames"] == 12
    assert payload["mask_pre_offset"] == 2
    assert payload["mask_post_offset"] == 3
    assert payload["guide_frame_snapshots"][0]["asset_id"] == "guide-1"
    assert payload["guide_frame_snapshots"][0]["muted"] is True
    assert payload["prompt_sections"][0]["prompt"] == "section prompt"
    assert payload["scene_width"] == 1024
    assert payload["scene_height"] == 576
    assert payload["scene_fps"] == 30.0
    assert payload["template_id"] == "ltxv-2.3"
    assert payload["frame_constraint"] == {"step": 8, "offset": 1, "min": 1, "max": 257}
    assert payload["take_placement_mode"] == "untrimmed"
    assert project.generation_queue[0].frame_constraint == {"step": 8, "offset": 1, "min": 1, "max": 257}
    assert project.generation_queue[0].take_placement_mode == "untrimmed"
    assert project.generation_queue[0].mask_pre_offset == 2
    assert project.generation_queue[0].mask_post_offset == 3
    assert payload["params"]["snapshot_version"] == 1


def test_delete_asset_route_soft_deletes_in_use_asset(tmp_path, monkeypatch):
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

    assert response.status == 200
    assert payload["trashed"] is True
    assert payload["asset_id"] == "asset-1"
    assert project.get_asset("asset-1") is asset
    assert asset.trashed_at
    assert asset.folder == ""
    assert scene.clips[0].source_path == "media/clip.mp4"


def test_restore_asset_route_restores_previous_folder(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)
    asset = Asset(
        asset_id="asset-1",
        asset_type="video",
        path="media/clip.mp4",
        name="Clip",
        folder="",
        trashed_at="2026-04-05T12:00:00",
        trash_previous_folder="Shots",
    )
    project.assets = [asset]
    project.metadata["asset_folders"] = ["Shots"]

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "save_project", lambda project: None)

    request = DummyRequest(match_info={"project_id": "phase-2"}, body={"asset_id": "asset-1"})
    response = asyncio.run(module.api_restore_asset(request))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["restored"] is True
    assert asset.trashed_at == ""
    assert asset.trash_previous_folder == ""
    assert asset.folder == "Shots"
    assert payload["asset"]["folder"] == "Shots"


def test_permanent_delete_asset_route_returns_409_for_in_use_asset(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)
    asset = Asset(asset_id="asset-1", asset_type="video", path="media/clip.mp4", name="Clip", trashed_at="2026-04-05T12:00:00")
    scene = Scene(scene_id="scene-1", name="Opening", order=1)
    scene.clips = [ClipReference(clip_id="clip-1", source_path="media/clip.mp4", timeline_start_frame=0, timeline_end_frame=24)]
    project.assets = [asset]
    project.scenes = [scene]

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "save_project", lambda project: None)

    request = DummyRequest(match_info={"project_id": "phase-2"}, body={"asset_id": "asset-1", "force": False})
    response = asyncio.run(module.api_permanent_delete_asset(request))
    payload = _response_json(response)

    assert response.status == 409
    assert payload["error"] == "Asset is in use"
    assert payload["usage_count"] == 1
    assert payload["usages"][0]["asset_id"] == "asset-1"
    assert project.get_asset("asset-1") is asset


def test_permanent_delete_asset_route_force_removes_asset_from_registry(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)
    asset = Asset(asset_id="asset-1", asset_type="video", path="media/clip.mp4", name="Clip", trashed_at="2026-04-05T12:00:00")
    scene = Scene(scene_id="scene-1", name="Opening", order=1)
    scene.clips = [ClipReference(clip_id="clip-1", source_path="media/clip.mp4", timeline_start_frame=0, timeline_end_frame=24)]
    scene.guide_frames = [GuideFrame(frame_index=12, asset_id="asset-1")]
    project.assets = [asset]
    project.scenes = [scene]
    _write_project_file(project, "media/clip.mp4", b"video")
    _write_asset_cache(project, asset.asset_id)

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "save_project", lambda project: None)

    request = DummyRequest(match_info={"project_id": "phase-2"}, body={"asset_id": "asset-1", "force": True})
    response = asyncio.run(module.api_permanent_delete_asset(request))
    payload = _response_json(response)

    assert response.status == 200
    assert payload["deleted"] is True
    assert payload["asset_id"] == "asset-1"
    assert payload["usages_orphaned"] == 2
    assert project.get_asset("asset-1") is None
    assert scene.clips[0].source_path == "media/clip.mp4"
    assert scene.guide_frames[0].asset_id == "asset-1"


def test_delete_asset_folder_route_soft_deletes_contained_assets(tmp_path, monkeypatch):
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

    assert response.status == 200
    assert payload["trashed_folder"] == "Shots"
    assert payload["trashed_assets"] == 2
    assert project.get_asset("a1").trashed_at
    assert project.get_asset("a2").trashed_at
    assert project.get_asset("a1").folder == ""
    assert project.get_asset("a2").folder == ""
    assert project.get_asset("a3").trashed_at == ""


def test_render_cache_routes_list_and_delete_project_cache_files(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)
    cache_dir = os.path.join(project.project_dir, "cache", "renders")
    os.makedirs(cache_dir, exist_ok=True)
    old_path = os.path.join(cache_dir, "scene-old.pt")
    new_path = os.path.join(cache_dir, "scene-new.pt")
    with open(old_path, "wb") as handle:
        handle.write(b"old")
    with open(new_path, "wb") as handle:
        handle.write(b"newer")
    with open(os.path.join(cache_dir, "not-render.txt"), "wb") as handle:
        handle.write(b"ignore")
    os.utime(old_path, (100, 100))
    os.utime(new_path, (200, 200))

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)

    response = asyncio.run(module.api_list_render_cache(DummyRequest(match_info={"project_id": "phase-2"})))
    payload = _response_json(response)

    assert response.status == 200
    assert [entry["filename"] for entry in payload] == ["scene-old.pt", "scene-new.pt"]
    assert payload[0]["size_bytes"] == 3

    delete_response = asyncio.run(module.api_delete_render_cache_entry(DummyRequest(
        match_info={"project_id": "phase-2", "filename": "scene-old.pt"},
    )))
    assert delete_response.status == 200
    assert not os.path.exists(old_path)
    assert os.path.exists(new_path)

    missing_response = asyncio.run(module.api_delete_render_cache_entry(DummyRequest(
        match_info={"project_id": "phase-2", "filename": "missing.pt"},
    )))
    assert missing_response.status == 404

    invalid_response = asyncio.run(module.api_delete_render_cache_entry(DummyRequest(
        match_info={"project_id": "phase-2", "filename": "..\\escape.pt"},
    )))
    assert invalid_response.status == 400


def test_trash_purge_honors_retention_days_and_decimal_mb_cap(tmp_path):
    project = _make_project(tmp_path)
    old_asset = Asset(
        asset_id="old",
        asset_type="video",
        path="media/old.mp4",
        trashed_at=(datetime.now() - timedelta(days=10)).isoformat(),
    )
    recent_oldest = Asset(
        asset_id="recent-oldest",
        asset_type="video",
        path="media/recent-oldest.mp4",
        trashed_at=(datetime.now() - timedelta(days=3)).isoformat(),
    )
    recent_middle = Asset(
        asset_id="recent-middle",
        asset_type="video",
        path="media/recent-middle.mp4",
        trashed_at=(datetime.now() - timedelta(days=2)).isoformat(),
    )
    recent_newest = Asset(
        asset_id="recent-newest",
        asset_type="video",
        path="media/recent-newest.mp4",
        trashed_at=(datetime.now() - timedelta(days=1)).isoformat(),
    )
    project.assets = [old_asset, recent_oldest, recent_middle, recent_newest]
    _write_project_file(project, "media/old.mp4", b"x" * 10)
    recent_oldest_path = _write_project_file(project, "media/recent-oldest.mp4", b"x" * 200)
    recent_middle_path = _write_project_file(project, "media/recent-middle.mp4", b"x" * 100)
    recent_newest_path = _write_project_file(project, "media/recent-newest.mp4", b"x" * 50)

    changed = routes._purge_expired_trashed_assets(
        project,
        retention_days=7,
        max_size_mb=0.00025,
    )

    assert changed is True
    assert project.get_asset("old") is None
    assert project.get_asset("recent-oldest") is None
    assert project.get_asset("recent-middle") is recent_middle
    assert project.get_asset("recent-newest") is recent_newest
    assert not os.path.exists(recent_oldest_path)
    assert os.path.exists(recent_middle_path)
    assert os.path.exists(recent_newest_path)


def test_asset_list_route_forwards_trash_retention_query_params(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    project = _make_project(tmp_path)
    captured = {}

    def fake_sync(sync_project, retention_days, max_size_mb):
        captured["project"] = sync_project
        captured["retention_days"] = retention_days
        captured["max_size_mb"] = max_size_mb
        return False

    monkeypatch.setattr(module, "_load_project_from_request", lambda request: project)
    monkeypatch.setattr(module, "_sync_media_folder", fake_sync)

    request = DummyRequest(
        match_info={"project_id": "phase-2"},
        query={"include_trashed": "true", "retention_days": "7", "max_size_mb": "1.5"},
    )
    response = asyncio.run(module.api_list_assets(request))

    assert response.status == 200
    assert captured == {
        "project": project,
        "retention_days": 7,
        "max_size_mb": 1.5,
    }
