"""Real-media fixtures for the Editor's render and audio mix (Execution Cache Pipeline Phase 4b).

Phase 4b moves the Editor's render and audio mix behind a recipe
(``nodes/editor_media.py``). These fixtures build one contained project on disk
with real media (lossless FFV1 video, PCM WAV at two rates, a PNG guide) and a
timeline that exercises every renderer and mixer input: two video tracks, a
clip with an integer opacity loaded from JSON, a half-opacity clip, every fit
mode, a muted clip, a hidden lane, a motion driver, a clip whose asset rate
differs from the scene's, audio at two volumes, a zero-volume track, an
audible track outside the window that still votes on the mix rate, and a
guide. ``cases`` runs the real ``SonderEditor.execute`` over live, queued
(consume and peek), padded, context-frame and render-cache-on variants;
``digest_result`` reduces what a run produced to JSON.

``modules`` is a namespace with ``timeline_state``, ``project_manager``,
``editor_node`` and ``media_helpers`` loaded the caller's way.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import wave
from pathlib import Path

import re

import numpy as np

import media_identity_fixtures as media

PROJECT = "editor-media-golden"
_HEX_ID = re.compile("[0-9a-f]{32}")
SCENE = "scene-1"


def write_wav(path: Path, rate: int, seconds: float, tone: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = int(seconds * rate)
    t = np.arange(count) / rate
    left = (np.sin(2 * math.pi * tone * t) * 12000).astype(np.int16)
    right = (np.sin(2 * math.pi * tone * 1.25 * t) * 9000).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.stack([left, right], axis=1).tobytes())
    return path


def build_project(m, root: Path, ffmpeg: str, *, name: str = PROJECT, corrupt_audio: bool = False,
                  frame_constraint: dict | None = None, queue: str = "", audio_constraint: dict | None = None):
    """Write the fixture project under ``root/sonder-projects/name`` and return its folder name."""
    ts = m.timeline_state
    folder = media.project_dir(root, name)
    media.write_video(folder / "media" / "a.mkv", ffmpeg, frames=40, tint=0)
    media.write_video(folder / "media" / "b.mkv", ffmpeg, frames=30, tint=91)
    media.write_video(folder / "media" / "drv.mkv", ffmpeg, frames=30, tint=33)
    write_wav(folder / "media" / "music.wav", 22050, 3.0, 220.0)
    write_wav(folder / "media" / "voice.wav", 32000, 2.0, 330.0)
    write_wav(folder / "media" / "late.wav", 44100, 1.0, 440.0)
    media.write_png(folder / "media" / "guide.png", 40, 24, tint=50)
    if corrupt_audio:
        (folder / "media" / "voice.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVEnot audio")
    width, height = media.VIDEO_SIZE
    assets = [
        ts.Asset(asset_id="va", name="a", asset_type="video", path="media/a.mkv", width=width, height=height,
                 fps=30.0, frame_count=40, duration_sec=40 / 30.0),
        # Declared at 20 fps: the renderer maps scene frames through the asset's rate.
        ts.Asset(asset_id="vb", name="b", asset_type="video", path="media/b.mkv", width=width, height=height,
                 fps=20.0, frame_count=30, duration_sec=30 / 20.0),
        ts.Asset(asset_id="vd", name="drv", asset_type="video", path="media/drv.mkv", width=width, height=height,
                 fps=30.0, frame_count=30, duration_sec=1.0),
        ts.Asset(asset_id="am", name="music", asset_type="audio", path="media/music.wav", duration_sec=3.0),
        ts.Asset(asset_id="av", name="voice", asset_type="audio", path="media/voice.wav", duration_sec=2.0),
        ts.Asset(asset_id="al", name="late", asset_type="audio", path="media/late.wav", duration_sec=1.0),
        ts.Asset(asset_id="g1", name="guide", asset_type="image", path="media/guide.png", width=40, height=24),
    ]
    clips = [
        ts.ClipReference(clip_id="c1", source_path="media/a.mkv", timeline_start_frame=0, timeline_end_frame=18,
                         source_in_frame=2, track_index=0, opacity=1, fit_mode="pad_edge"),
        ts.ClipReference(clip_id="c2", source_path="media/b.mkv", timeline_start_frame=6, timeline_end_frame=30,
                         source_in_frame=1, track_index=1, opacity=0.5, fit_mode="cover", crop_position="top"),
        ts.ClipReference(clip_id="c3", source_path="media/a.mkv", timeline_start_frame=18, timeline_end_frame=40,
                         source_in_frame=5, track_index=0, fit_mode="stretch"),
        ts.ClipReference(clip_id="c4", source_path="media/b.mkv", timeline_start_frame=0, timeline_end_frame=40,
                         track_index=1, fit_mode="fit", muted=True),
        ts.ClipReference(clip_id="c5", source_path="media/a.mkv", timeline_start_frame=4, timeline_end_frame=36,
                         track_index=2, opacity=0.8, fit_mode="fit"),
        ts.ClipReference(clip_id="d1", source_path="media/drv.mkv", timeline_start_frame=2, timeline_end_frame=28,
                         track_index=0, role="motion_driver"),
    ]
    audio = [
        ts.AudioTrack(track_id="t1", source_path="media/music.wav", timeline_start_frame=0, timeline_end_frame=40,
                      source_in_frame=3, volume=0.6, lane_index=0),
        ts.AudioTrack(track_id="t2", source_path="media/voice.wav", timeline_start_frame=10, timeline_end_frame=26,
                      volume=1, lane_index=1),
        ts.AudioTrack(track_id="t3", source_path="media/music.wav", timeline_start_frame=20, timeline_end_frame=30,
                      volume=0.0, lane_index=2),
        # Outside every window the cases render, yet audible: it votes on the mix rate.
        ts.AudioTrack(track_id="t4", source_path="media/late.wav", timeline_start_frame=44, timeline_end_frame=60,
                      volume=0.9, lane_index=0),
    ]
    guides = [ts.GuideFrame(guide_id="gd1", frame_index=12, asset_id="g1", source="asset", strength=0.75,
                            fit_mode="cover")]
    video_configs = [ts.LaneConfig(), ts.LaneConfig(), ts.LaneConfig(), ts.LaneConfig(hidden=True)]
    clips.append(ts.ClipReference(clip_id="c6", source_path="media/b.mkv", timeline_start_frame=0,
                                  timeline_end_frame=40, track_index=3, fit_mode="stretch"))
    scene = ts.Scene(scene_id=SCENE, name="Scene 1", duration_frames=60, clips=clips, audio_tracks=audio,
                     guide_frames=guides, video_lane_count=4, video_lane_configs=video_configs,
                     audio_lane_count=3, audio_lane_configs=[ts.LaneConfig(), ts.LaneConfig(), ts.LaneConfig()],
                     fps=24.0, width=16, height=12)
    scene.set_global_prompt("a quiet street") if hasattr(scene, "set_global_prompt") else None
    project = ts.TimelineProject(project_dir=str(folder), name=name, fps=24.0, resolution=(16, 12), scenes=[scene],
                                 assets=assets)
    if frame_constraint is not None:
        project.frame_constraint = dict(frame_constraint)
    if audio_constraint is not None:
        project.audio_constraint = dict(audio_constraint)
    if queue:
        job = ts.GenerationJob(
            job_id="job-1", scene_id=SCENE, status="pending", selection_start=8, selection_end=28,
            pre_context_frames=4, post_context_frames=6, prompt="queued prompt",
            params={"snapshot_version": 1}, scene_fps=24.0, scene_width=20, scene_height=14,
            guide_frame_snapshots=[guides[0].to_dict()], driver_clip_snapshots=[clips[5].to_dict()],
            driver_lane_count=1, driver_lane_configs=[ts.LaneConfig().to_dict()],
            frame_constraint=dict(frame_constraint) if frame_constraint else None)
        project.generation_queue = [job]
    m.project_manager.save_project(project)
    return name


def _digest_tensor(tensor) -> dict:
    array = tensor.detach().cpu().contiguous().numpy()
    return {"shape": list(array.shape), "dtype": str(array.dtype),
            "sha256": hashlib.sha256(array.tobytes()).hexdigest()}


def _jsonable(value):
    try:
        import torch
    except Exception:  # pragma: no cover
        torch = None
    if torch is not None and isinstance(value, torch.Tensor):
        return _digest_tensor(value)
    if isinstance(value, np.ndarray):
        return {"shape": list(value.shape), "dtype": str(value.dtype),
                "sha256": hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest()}
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        # A project's generated id differs on every build of the fixture.
        return _HEX_ID.sub("<id>", value)
    if value is None or isinstance(value, (bool, int)):
        return value
    return f"<{type(value).__name__}>"


def digest_result(result) -> list:
    """Every output after ``project``, reduced to JSON (tensors by shape, dtype and bytes)."""
    return [_jsonable(value) for value in tuple(result)[1:]]


def context_of(project) -> dict:
    context = dict(getattr(project, "_execution_context", None) or {})
    context.pop("base_modified_at", None)
    return _jsonable(context)


def job_state(project_dir: str, m) -> list:
    loaded = m.project_manager.load_project(project_dir)
    return [{"job_id": job.job_id, "status": job.status, "error": job.error}
            for job in getattr(loaded, "generation_queue", []) or []]


def prompt_graph(save: bool) -> dict:
    graph = {"editor-1": {"class_type": "SonderEditor", "inputs": {}},
             "frames": {"class_type": "Consumer", "inputs": {"images": ["editor-1", 1]}},
             "guides": {"class_type": "Consumer", "inputs": {"images": ["editor-1", 2], "idx": ["editor-1", 3]}},
             "audio": {"class_type": "Consumer", "inputs": {"audio": ["editor-1", 10]}}}
    if save:
        graph["save-1"] = {"class_type": "SonderSaveVideo",
                           "inputs": {"project": ["editor-1", 0], "mark_queue_complete": True}}
    return graph


LTX_CONSTRAINT = {"step": 8, "offset": 1, "min": 9}


def cases():
    """``(label, build kwargs, execute kwargs)`` for every golden case."""
    base = {"selection_start": 0, "selection_end": 0, "render_queue_active": False}
    return [
        ("live_full_uncached", {}, dict(base, render_cache_max_bytes=0)),
        ("live_full_cached", {}, dict(base, render_cache_max_bytes=-1)),
        ("live_selection_context", {}, dict(base, selection_start=8, selection_end=28, pre_context_frames=3,
                                            post_context_frames=5, render_cache_max_bytes=0)),
        ("live_selection_context_cached", {}, dict(base, selection_start=8, selection_end=28,
                                                   pre_context_frames=3, post_context_frames=5,
                                                   render_cache_max_bytes=-1)),
        ("live_padding_ltx", {"frame_constraint": LTX_CONSTRAINT},
         dict(base, selection_start=5, selection_end=19, render_cache_max_bytes=0)),
        ("queue_consume_frozen", {"queue": "consume", "frame_constraint": LTX_CONSTRAINT},
         dict(render_queue_active=True, render_cache_max_bytes=0, save=True)),
        ("queue_peek_frozen", {"queue": "peek"}, dict(render_queue_active=True, render_cache_max_bytes=-1,
                                                      save=False)),
    ]


def summary(m, root: Path, ffmpeg: str, label: str, build_kwargs: dict, execute_kwargs: dict, *,
            editor=None, deferred=False) -> dict:
    """Run one case on a fresh copy of the fixture project and reduce it to JSON.

    ``deferred``: the Editor defers every slot it can identify (as the cache
    pipeline's early run asks), then each is identified and settled from its
    recipe (``PendingMedia.identify`` and ``value``), as a miss settles them.
    """
    execute_kwargs = dict(execute_kwargs)
    save = execute_kwargs.pop("save", None)
    name = build_project(m, root, ffmpeg, name=f"{PROJECT}-{label}", **build_kwargs)
    notes: list = []
    original_note = m.editor_node._note_run_feedback
    original_record = m.editor_node.run_outputs.note_editor_context
    records: list = []

    def note(method, prompt, unique_id, *args):
        notes.append([method, _jsonable(args)])

    def record(prompt, unique_id, fields, content=None):
        fields = dict(fields)
        fields.pop("start_modified_at", None)  # the project's save time differs on every build
        records.append(_jsonable(fields))

    m.editor_node._note_run_feedback = note
    m.editor_node.run_outputs.note_editor_context = record
    kept = []
    media_module = m.editor_node.editor_media
    sdk = m.editor_node.sdk_runs
    original_requested, original_remember = media_module.deferral_requested, sdk.remember_pending
    if deferred:
        media_module.deferral_requested = lambda unique_id: True
        sdk.remember_pending = lambda key, call_key, pending: kept.append(pending) or True
    try:
        node = editor or m.editor_node.SonderEditor()
        prompt = prompt_graph(bool(save)) if save is not None else None
        try:
            result = node.execute(project=name, project_name="", fps=24.0, width=16, height=12,
                                  scene_id=SCENE, prompt=prompt, unique_id="editor-1" if prompt else None,
                                  **execute_kwargs)
            if deferred:
                assert kept and any(media_module.is_deferred(value) for value in result), "nothing was deferred"
                values = list(result)
                for slot, value in enumerate(values):
                    if media_module.is_deferred(value):
                        token, reason = kept[0].identify(slot)
                        assert token, reason
                        values[slot] = kept[0].value(slot)
                result = tuple(values)
            outcome = {"outputs": digest_result(result), "context": context_of(result[0])}
        except BaseException as exc:  # noqa: BLE001 - the failure path is part of the golden record
            outcome = {"raised": type(exc).__name__, "message": str(exc)}
    finally:
        m.editor_node._note_run_feedback = original_note
        m.editor_node.run_outputs.note_editor_context = original_record
        media_module.deferral_requested, sdk.remember_pending = original_requested, original_remember
    project_dir = str(root / "sonder-projects" / name)
    outcome["jobs"] = job_state(project_dir, m)
    outcome["notes"] = notes
    outcome["views"] = records
    return outcome


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def scene_paths(root: Path, label: str) -> dict:
    folder = root / "sonder-projects" / f"{PROJECT}-{label}"
    return {"folder": str(folder), "media": str(folder / "media")}


def cleanup_env():
    os.environ.pop("SONDER_CAPTURE_EDITOR_GOLDEN", None)
