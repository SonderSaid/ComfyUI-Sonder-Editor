"""Real-media fixtures for the identified bridges (Execution Cache Pipeline Phase 3).

Every Driver, Reference Image, Reference Audio and Guides Bridge Start case
decodes real files: PNG stills written by OpenCV, FFV1 video encoded by the
pack's own ffmpeg (single-threaded, lossless, so the bytes are deterministic),
and 16-bit WAV audio. Each case is built inside a contained
``<root>/sonder-projects/<name>`` project and returns ``(project, run)``: ``run``
decodes through the public functions the bridges call, and gives the outputs
in the bridge's output order.

``modules`` is a namespace with the pack modules loaded the caller's way:
``timeline_state``, ``project_manager``, ``driver``, ``reference_core``,
``guides`` and ``media_helpers``.
"""

from __future__ import annotations

import math
import os
import subprocess
import wave
from pathlib import Path

import cv2
import numpy as np

VIDEO_SIZE = (32, 24)  # width, height
VIDEO_FRAMES = 30
VIDEO_FPS = 30.0
AUDIO_RATE = 22050


def _frame(index: int, width: int, height: int, tint: int = 0) -> np.ndarray:
    """A deterministic RGB frame whose pixels encode its index."""
    y, x = np.mgrid[0:height, 0:width]
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    frame[..., 0] = (x * 7 + index * 9 + tint) % 256
    frame[..., 1] = (y * 11 + index * 5) % 256
    frame[..., 2] = ((x + y) * 3 + index * 13 + tint * 2) % 256
    return frame


def write_png(path: Path, width: int = 40, height: int = 24, tint: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(path), _frame(0, width, height, tint)[:, :, ::-1])
    return path


def write_video(path: Path, ffmpeg: str, frames: int = VIDEO_FRAMES, tint: int = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    width, height = VIDEO_SIZE
    raw = b"".join(_frame(index, width, height, tint).tobytes() for index in range(frames))
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "-s", f"{width}x{height}", "-r", str(int(VIDEO_FPS)), "-i", "-", "-threads", "1",
         "-c:v", "ffv1", "-pix_fmt", "yuv420p", "-fflags", "+bitexact", "-flags", "+bitexact",
         "-map_metadata", "-1", str(path)],
        input=raw, check=True, capture_output=True)
    return path


def write_wav(path: Path, seconds: float = 1.5, tone: float = 220.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = int(seconds * AUDIO_RATE)
    t = np.arange(count) / AUDIO_RATE
    left = (np.sin(2 * math.pi * tone * t) * 12000).astype(np.int16)
    right = (np.sin(2 * math.pi * tone * 1.5 * t) * 8000).astype(np.int16)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(AUDIO_RATE)
        handle.writeframes(np.stack([left, right], axis=1).tobytes())
    return path


def project_dir(root: Path, name: str) -> Path:
    path = root / "sonder-projects" / name
    (path / "media").mkdir(parents=True, exist_ok=True)
    return path


def video_asset(m, asset_id: str, rel: str, *, fps: float = VIDEO_FPS, frames: int = VIDEO_FRAMES):
    width, height = VIDEO_SIZE
    return m.timeline_state.Asset(asset_id=asset_id, name=asset_id, asset_type="video", path=rel, width=width,
                                  height=height, fps=fps, frame_count=frames, duration_sec=frames / fps)


def _saved_and_loaded(m, project):
    """Save and load back: a frozen job then hydrates its snapshot from disk on first read."""
    context = dict(project._execution_context)
    m.project_manager.save_project(project)
    loaded = m.project_manager.load_project(project.project_dir)
    loaded._execution_context = context
    return loaded


# --- Driver ----------------------------------------------------------------------------------


def _driver_project(m, root: Path, name: str, ffmpeg: str, *, fit="pad_edge", muted=False, asset_fps=VIDEO_FPS,
                    window=(5, 15), padding=0, frozen=False):
    ts = m.timeline_state
    folder = project_dir(root, name)
    write_video(folder / "media" / "driver.mkv", ffmpeg, tint=17)
    clip = ts.ClipReference(clip_id="driver-1", source_path="media/driver.mkv", timeline_start_frame=3,
                            timeline_end_frame=20, source_in_frame=2, track_index=0, role="motion_driver",
                            strength=0.42, muted=muted, fit_mode=fit, crop_position="center")
    scene = ts.Scene(scene_id="scene-1", name="Scene 1", duration_frames=24, motion_driver_lane_count=1,
                     motion_driver_lane_configs=[ts.LaneConfig()], clips=[clip], fps=24.0)
    project = ts.TimelineProject(project_dir=str(folder), name=name, fps=24.0, resolution=(16, 12), scenes=[scene],
                                 assets=[video_asset(m, "drv", "media/driver.mkv", fps=asset_fps)])
    start, end = window
    project._execution_context = {"scene_id": "scene-1", "context_start": start, "context_end": end,
                                  "frame_count": end - start + padding, "frame_count_padding": padding,
                                  "queue_job_id": "", "queue_job_ref_id": ""}
    if frozen:
        project.generation_queue = [ts.GenerationJob(
            job_id="job-1", scene_id="scene-1", params={"snapshot_version": 1}, scene_fps=24.0,
            driver_clip_snapshots=[clip.to_dict()], driver_lane_count=1,
            driver_lane_configs=[ts.LaneConfig().to_dict()])]
        project._execution_context["queue_job_ref_id"] = "job-1"
        # The live clip moves after enqueue: the frozen snapshot must win.
        scene.clips[0].source_in_frame = 9
        project = _saved_and_loaded(m, project)
    return project


def _run_driver(m, lane=0):
    def run(project):
        ref = m.driver.SonderDriverSelector().execute(project, lane)[0]
        return tuple(m.driver.SonderDriverBridge().execute(ref))
    return run


# --- Reference ---------------------------------------------------------------------------------


def _preset(m, recipe_id: str):
    ts = m.timeline_state
    rows = list(ts.REFERENCE_RECIPE_PRESETS) + list(ts.MINIMAX_H3_REFERENCE_RECIPE_PRESETS)
    row = next(row for row in rows if row["id"] == recipe_id)
    return ts.ReferenceLaneRecipe(media_kind=row["media_kind"], recipe_id=row["id"],
                                  recipe={"name": row["name"], "hard": dict(row["hard"]), "soft": dict(row["soft"])})


def _reference_project(m, root: Path, name: str, ffmpeg: str, recipe_id: str, members: list[dict], *,
                       frozen=False, sequence_frames=0):
    """``members``: ``{"kind": "png"|"video"|"wav", "crop", "start", "end", "context"}`` per staged member."""
    ts = m.timeline_state
    folder = project_dir(root, name)
    assets, references, staged = [], [], []
    for index, spec in enumerate(members):
        kind = spec["kind"]
        if kind == "png":
            rel = f"media/member{index}.png"
            write_png(folder / rel, tint=index * 40)
            asset = ts.Asset(asset_id=f"a{index}", name=f"A{index}", asset_type="image", path=rel, width=40, height=24)
        elif kind == "video":
            rel = f"media/member{index}.mkv"
            write_video(folder / rel, ffmpeg, tint=index * 40)
            asset = video_asset(m, f"a{index}", rel)
        else:
            rel = f"media/member{index}.wav"
            write_wav(folder / rel, tone=220.0 + 110.0 * index)
            asset = ts.Asset(asset_id=f"a{index}", name=f"A{index}", asset_type="audio", path=rel,
                             duration_sec=1.5, sample_rate=AUDIO_RATE)
        assets.append(asset)
        member = ts.ReferenceMember(member_id=f"m{index}", asset_id=f"a{index}", name=f"member{index}",
                                    prompt=f"member {index}", crop=spec.get("crop"),
                                    source_start_sec=spec.get("start", 0.0), source_end_sec=spec.get("end"))
        references.append(ts.ReferenceEntity(reference_id=f"e{index}", name=f"Entity{index}", members=[member],
                                             reference_class="context" if spec.get("context") else "subject"))
        staged.append({"entity_id": f"e{index}", "member_id": f"m{index}"})
    recipe = _preset(m, recipe_id)
    item = ts.ReferenceItem(reference_item_id="item", lane_index=0, start_frame=0, end_frame=-1, members=staged,
                            sequence_frames=sequence_frames)
    scene = ts.Scene(scene_id="scene", duration_frames=60, width=64, height=48, fps=24.0, reference_lane_count=1,
                     reference_lane_configs=[ts.LaneConfig()], reference_lane_recipes=[recipe],
                     reference_items=[item])
    project = ts.TimelineProject(project_dir=str(folder), project_id=name, resolution=(64, 48), scenes=[scene],
                                 assets=assets, references=references, fps=24.0)
    project._execution_context = {"scene_id": "scene", "context_start": 12, "context_end": 36,
                                  "queue_job_ref_id": ""}
    if frozen:
        project.generation_queue = [ts.GenerationJob(
            job_id="queued", scene_id="scene", params={"snapshot_version": 1}, scene_fps=24.0,
            reference_lane_count=1, reference_lane_configs=[ts.LaneConfig().to_dict()],
            reference_lane_recipes=[recipe.to_dict()], reference_item_snapshots=[item.to_dict()],
            reference_input_snapshots=[{"kind": "reference", "value": ref.to_dict()} for ref in references]
            + [{"kind": "asset", "value": asset.to_dict()} for asset in assets])]
        project._execution_context["queue_job_ref_id"] = "queued"
        # Live edits after enqueue: the frozen catalog must win.
        project.references[0].members[0].crop = {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5}
        project = _saved_and_loaded(m, project)
    return project


def _run_reference_images(m, unused="placeholder"):
    def run(project):
        ref = m.reference_core.resolve_reference_set(project, "0")
        return tuple(m.reference_core.decode_reference_images(ref, unused))
    return run


def _run_reference_audios(m, unused="placeholder"):
    def run(project):
        ref = m.reference_core.resolve_reference_set(project, "0")
        return tuple(m.reference_core.decode_reference_audios(ref, unused))
    return run


# --- Guides ------------------------------------------------------------------------------------


def _guides_project(m, root: Path, name: str, ffmpeg: str, kinds: list[str], *, frozen=False):
    ts = m.timeline_state
    folder = project_dir(root, name)
    assets, guides = [], []
    for index, kind in enumerate(kinds):
        if kind == "video":
            rel = f"media/guide{index}.mkv"
            write_video(folder / rel, ffmpeg, tint=60 + index * 30)
            assets.append(video_asset(m, f"g{index}", rel))
        else:
            rel = f"media/guide{index}.png"
            write_png(folder / rel, width=30, height=20, tint=60 + index * 30)
            assets.append(ts.Asset(asset_id=f"g{index}", name=f"G{index}", asset_type="image", path=rel,
                                   width=30, height=20))
        guides.append(ts.GuideFrame(guide_id=f"guide{index}", frame_index=14 + index * 4, asset_id=f"g{index}",
                                    strength=0.5 + index * 0.125, fit_mode=("pad_edge", "cover", "fit")[index % 3],
                                    crop_position="center"))
    scene = ts.Scene(scene_id="scene", duration_frames=48, width=36, height=20, fps=24.0, guide_frames=guides)
    project = ts.TimelineProject(project_dir=str(folder), project_id=name, resolution=(36, 20), scenes=[scene],
                                 assets=assets, fps=24.0)
    project._execution_context = {"scene_id": "scene", "context_start": 12, "context_end": 36,
                                  "queue_job_ref_id": ""}
    if frozen:
        project.generation_queue = [ts.GenerationJob(
            job_id="queued", scene_id="scene", params={"snapshot_version": 1}, scene_fps=24.0,
            guide_frame_snapshots=[guide.to_dict() for guide in guides])]
        project._execution_context["queue_job_ref_id"] = "queued"
        scene.guide_frames = scene.guide_frames[:1]  # live edit after enqueue
        project = _saved_and_loaded(m, project)
    return project


def _run_guides(m, iterations: int, overrides="{}"):
    def run(project):
        outputs = []
        for index in range(max(1, iterations)):
            out = m.guides.SonderGuidesBridgeStart().execute(project, index, bridge_overrides_json=overrides)
            outputs.extend(out[1:4])
        return tuple(outputs)
    return run


# --- The case list ----------------------------------------------------------------------------


def cases(m, root: Path, ffmpeg: str):
    """``[(label, family, build, spec)]``; ``build()`` returns ``(project, run)``.

    ``spec`` says how the bridge is called: ``lane`` (Driver), ``unused``
    (Reference ``unused_slots``), ``overrides`` and ``iterations`` (Guides).
    """
    root = Path(root)

    def driver_case(label, make, lane=0):
        return (label, "driver", lambda: (make(), _run_driver(m, lane)), {"lane": lane})

    def image_case(label, make, unused="placeholder"):
        return (label, "reference_image", lambda: (make(), _run_reference_images(m, unused)), {"unused": unused})

    def audio_case(label, make, unused="placeholder"):
        return (label, "reference_audio", lambda: (make(), _run_reference_audios(m, unused)), {"unused": unused})

    def guides_case(label, make, iterations, overrides="{}"):
        return (label, "guides", lambda: (make(), _run_guides(m, iterations, overrides)),
                {"iterations": iterations, "overrides": overrides})

    return [
        driver_case("driver present", lambda: _driver_project(m, root, "d1", ffmpeg)),
        driver_case("driver cover rate tail",
                    lambda: _driver_project(m, root, "d2", ffmpeg, fit="cover", asset_fps=30.0, window=(5, 20),
                                            padding=3)),
        driver_case("driver muted", lambda: _driver_project(m, root, "d3", ffmpeg, muted=True)),
        driver_case("driver absent lane", lambda: _driver_project(m, root, "d4", ffmpeg), lane=1),
        driver_case("driver frozen from disk", lambda: _driver_project(m, root, "d5", ffmpeg, frozen=True)),
        image_case("reference batch still crop",
                   lambda: _reference_project(m, root, "r1", ffmpeg, "sonder:wan_phantom",
                                              [{"kind": "png", "crop": {"x": 0.1, "y": 0.2, "w": 0.6, "h": 0.5}},
                                               {"kind": "png"}])),
        image_case("reference slots video span",
                   lambda: _reference_project(m, root, "r2", ffmpeg, "sonder:minimax_h3_video",
                                              [{"kind": "video", "start": 0.2, "end": 0.6}, {"kind": "png"}])),
        image_case("reference sheet",
                   lambda: _reference_project(m, root, "r3", ffmpeg, "sonder:ltx_ingredients",
                                              [{"kind": "png"}, {"kind": "video", "start": 0.3}])),
        image_case("reference strip",
                   lambda: _reference_project(m, root, "r4", ffmpeg, "sonder:wan_vace",
                                              [{"kind": "png"}, {"kind": "png"}])),
        image_case("reference temporal",
                   lambda: _reference_project(m, root, "r5", ffmpeg, "sonder:ltx_msr",
                                              [{"kind": "png", "context": True}, {"kind": "png"}], sequence_frames=17)),
        image_case("reference slots nothing",
                   lambda: _reference_project(m, root, "r6", ffmpeg, "sonder:minimax_h3_picture", [{"kind": "png"}]),
                   unused="nothing"),
        image_case("reference frozen from disk",
                   lambda: _reference_project(m, root, "r7", ffmpeg, "sonder:minimax_h3_picture",
                                              [{"kind": "png", "crop": {"x": 0.0, "y": 0.0, "w": 0.5, "h": 1.0}}],
                                              frozen=True)),
        audio_case("reference audio",
                   lambda: _reference_project(m, root, "a1", ffmpeg, "sonder:minimax_h3_audio",
                                              [{"kind": "wav", "start": 0.25, "end": 1.0}, {"kind": "wav"}])),
        audio_case("reference audio nothing",
                   lambda: _reference_project(m, root, "a2", ffmpeg, "sonder:ltx_id_lora_audio", [{"kind": "wav"}]),
                   unused="nothing"),
        guides_case("guides none", lambda: _guides_project(m, root, "g0", ffmpeg, []), 1),
        guides_case("guides one", lambda: _guides_project(m, root, "g1", ffmpeg, ["png"]), 1),
        guides_case("guides three", lambda: _guides_project(m, root, "g3", ffmpeg, ["png", "video", "png"]), 3),
        guides_case("guides three muted one",
                    lambda: _guides_project(m, root, "g4", ffmpeg, ["png", "video", "png"]), 2,
                    overrides='{"guides": {"g1:18": true}}'),
        guides_case("guides frozen from disk",
                    lambda: _guides_project(m, root, "g5", ffmpeg, ["png", "video"], frozen=True), 2),
    ]


def digest(values) -> str:
    """A stable digest of bridge outputs: tensors by dtype, shape and bytes."""
    import hashlib

    import torch

    hasher = hashlib.sha256()

    def feed(value):
        if isinstance(value, torch.Tensor):
            tensor = value.detach().cpu().contiguous()
            hasher.update(f"T{tensor.dtype}{tuple(tensor.shape)}".encode())
            hasher.update(tensor.numpy().tobytes())
        elif isinstance(value, dict):
            hasher.update(b"D")
            for key in sorted(value):
                hasher.update(str(key).encode())
                feed(value[key])
        elif isinstance(value, (list, tuple)):
            hasher.update(f"L{len(value)}".encode())
            for item in value:
                feed(item)
        else:
            hasher.update(f"V{type(value).__name__}:{value!r}".encode())

    feed(values)
    return hasher.hexdigest()
