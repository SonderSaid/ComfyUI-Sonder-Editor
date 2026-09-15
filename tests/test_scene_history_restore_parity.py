"""Mutation-produced geometry must survive a history restore without repair."""
import copy

import pytest

from test_phase43_routes import _load_route_module
from test_history_validation import restore_case
from server.timeline_state import Asset, Scene, TimelineProject


def test_mutation_produced_geometry_is_restorable(tmp_path, monkeypatch):
    module = _load_route_module(monkeypatch)
    corpus = []
    for case in ("clip_overhang", "audio_overhang", "overlap", "negative", "prompt_overhang", "guide_overhang", "stranded_lane"):
        scene = Scene(scene_id="scene-1", duration_frames=10)
        project = TimelineProject(project_id="project", project_dir=str(tmp_path), scenes=[scene], assets=[
            Asset(asset_id="video", asset_type="video", path="media/video.mp4", fps=24, frame_count=24, duration_sec=1, has_audio=False),
            Asset(asset_id="audio", asset_type="audio", path="media/audio.wav", duration_sec=1, duration_checked=True),
        ])
        if case == "audio_overhang":
            module._apply_create_audio_track(project, scene, {"asset_id": "audio", "timeline_start_frame": 8})
        elif case == "prompt_overhang":
            module._apply_create_prompt_section(scene, {"start_frame": -2, "end_frame": 12})
        elif case == "guide_overhang":
            module._apply_create_guide(scene, {"frame_index": 12})
        else:
            clip, _, _ = module._apply_create_clip(project, scene, {"asset_id": "video", "timeline_start_frame": 0})
            if case == "negative":
                module._apply_update_clip(project, scene, clip.clip_id, {"timeline_start_frame": -3})
            elif case == "stranded_lane":
                module._apply_update_clip(project, scene, clip.clip_id, {"track_index": 9})
            elif case == "overlap":
                second, _, _ = module._apply_create_clip(project, scene, {"asset_id": "video", "timeline_start_frame": 30})
                module._apply_update_clip(project, scene, second.clip_id, {"timeline_start_frame": 2, "timeline_end_frame": 6})
        raw = scene.to_dict()
        corpus.append(raw)
        base = copy.deepcopy(raw)
        base["name"] = "Changed name"
        before_inputs = copy.deepcopy((base, raw))
        status, payload, _ = restore_case(monkeypatch, tmp_path, base, raw, base)
        assert status == 200, (case, payload)
        for collection in ("clips", "audio_tracks", "prompt_sections", "guide_frames"):
            assert payload["scene"][collection] == raw[collection], case
        assert (base, raw) == before_inputs
    # Non-trivial corpus: fail if future writer clamping erases the premise.
    overhangs = [any(item["timeline_end_frame"] > raw["duration_frames"]
                    for item in raw["clips"] + raw["audio_tracks"]) for raw in corpus]
    overlaps = [any(a["timeline_start_frame"] < b["timeline_end_frame"] and a["timeline_end_frame"] > b["timeline_start_frame"]
                    for i, a in enumerate(raw["clips"]) for b in raw["clips"][i + 1:]) for raw in corpus]
    assert any(overhangs) and not all(overhangs)
    assert any(overlaps) and not all(overlaps)
