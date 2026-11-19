"""Tests for SonderMasksBridge per-channel mask-time gating and mask compiling.

The bridge's resolution core is pure-Python (no relative imports), so it imports
through the fake package like the other bridge tests. Only the tensor emitters
need torch, so those tests guard with `pytest.importorskip("torch")` per-test
rather than at module level — the span/geometry tests below are the bulk of the
coverage and must still run in a bare environment.
"""

import importlib
import json
import math
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "video_editor_testpkg"

sys.path.insert(0, str(ROOT))

from server.timeline_state import GenerationJob, Scene, TimelineProject  # noqa: E402


def _import_masks_bridge():
    if TEST_PACKAGE not in sys.modules:
        pkg = types.ModuleType(TEST_PACKAGE)
        pkg.__path__ = [str(ROOT)]
        sys.modules[TEST_PACKAGE] = pkg
    importlib.invalidate_caches()
    return importlib.import_module(f"{TEST_PACKAGE}.nodes.masks_bridge")


def _project(*, project_fps=24.0, scene_fps=0.0, mask_start=12, mask_end=36,
             ctx="default", queue_job=None, frame_count=241):
    """Build a project + execution context for the bridge.

    ctx="default" -> a valid context with the mask frames;
    ctx="missing-keys" -> a context dict without the mask keys;
    ctx=None -> no execution context at all.
    """
    scene = Scene(scene_id="scene-1", name="Scene", duration_frames=100)
    scene.fps = scene_fps
    project = TimelineProject(project_dir="", name="Project", fps=project_fps, scenes=[scene])
    if queue_job is not None:
        project.generation_queue = [queue_job]
    if ctx == "default":
        project._execution_context = {
            "scene_id": "scene-1",
            "mask_start_frame": mask_start,
            "mask_end_frame": mask_end,
            "frame_count": frame_count,
            "queue_job_ref_id": queue_job.job_id if queue_job is not None else "",
        }
    elif ctx == "missing-keys":
        project._execution_context = {"scene_id": "scene-1"}
    # ctx is None -> leave _execution_context unset
    return project, scene


# ── Fake VAEs / latents. Only `.shape` and the geometry attributes are read, ──
# ── so no real model, no torch, and no ComfyUI are needed for these.         ──

# comfy/sd.py:746 (LTX 2.x diffusion VAE) and :994 (MiniMax H3 video VAE).
LTX_VIDEO_MAP = lambda a: max(0, math.floor((a + 7) / 8))            # noqa: E731
H3_VIDEO_MAP = lambda a: (max(1, (a - 5) // 17 * 5 + 2) if a > 1 else 1)  # noqa: E731


def _video_vae(fn=LTX_VIDEO_MAP):
    return types.SimpleNamespace(downscale_ratio=(fn, 32, 32))


def _audio_vae(rate=25.0):
    return types.SimpleNamespace(
        first_stage_model=types.SimpleNamespace(latents_per_second=rate))


def _latent(shape, nested=False):
    return {"samples": types.SimpleNamespace(shape=tuple(shape), is_nested=nested)}


# ── Existing seconds behaviour (unchanged) ───────────────────────────────────


def test_both_edit_passes_full_window():
    mb = _import_masks_bridge()
    project, _ = _project()  # fps 24, mask 12..36 -> 0.5s .. 1.5s
    r = mb.resolve_mask_times(project, edit_video=True, edit_audio=True)
    assert r["video_mask_start_time"] == pytest.approx(0.5)
    assert r["video_mask_end_time"] == pytest.approx(1.5)
    assert r["audio_mask_start_time"] == pytest.approx(0.5)
    assert r["audio_mask_end_time"] == pytest.approx(1.5)


def test_freeze_video_collapses_video_only():
    mb = _import_masks_bridge()
    project, _ = _project()
    r = mb.resolve_mask_times(project, edit_video=False, edit_audio=True)
    # video collapses to start; audio untouched
    assert r["video_mask_start_time"] == pytest.approx(0.5)
    assert r["video_mask_end_time"] == pytest.approx(0.5)
    assert r["audio_mask_start_time"] == pytest.approx(0.5)
    assert r["audio_mask_end_time"] == pytest.approx(1.5)
    # the frame-space window follows the same gate
    assert r["video_mask_frames"] == (12, 12)
    assert r["audio_mask_frames"] == (12, 36)


def test_freeze_audio_collapses_audio_only():
    mb = _import_masks_bridge()
    project, _ = _project()
    r = mb.resolve_mask_times(project, edit_video=True, edit_audio=False)
    assert r["video_mask_start_time"] == pytest.approx(0.5)
    assert r["video_mask_end_time"] == pytest.approx(1.5)
    assert r["audio_mask_start_time"] == pytest.approx(0.5)
    assert r["audio_mask_end_time"] == pytest.approx(0.5)


def test_both_freeze_collapses_both():
    mb = _import_masks_bridge()
    project, _ = _project()
    r = mb.resolve_mask_times(project, edit_video=False, edit_audio=False)
    assert r["video_mask_start_time"] == pytest.approx(0.5)
    assert r["video_mask_end_time"] == pytest.approx(0.5)
    assert r["audio_mask_start_time"] == pytest.approx(0.5)
    assert r["audio_mask_end_time"] == pytest.approx(0.5)


def test_scene_fps_override_drives_seconds():
    mb = _import_masks_bridge()
    project, _ = _project(project_fps=24.0, scene_fps=48.0)  # 12/48=0.25, 36/48=0.75
    r = mb.resolve_mask_times(project)
    assert r["video_mask_start_time"] == pytest.approx(0.25)
    assert r["video_mask_end_time"] == pytest.approx(0.75)


def test_queued_snapshot_frozen_fps_wins_over_live_scene():
    mb = _import_masks_bridge()
    job = GenerationJob(
        job_id="job-1", scene_id="scene-1", scene_fps=12.0,
        params={"snapshot_version": 1},
    )
    # Live scene fps is 48, but the frozen snapshot fps (12) must win.
    project, _ = _project(project_fps=24.0, scene_fps=48.0, queue_job=job)
    r = mb.resolve_mask_times(project)
    assert r["video_mask_start_time"] == pytest.approx(1.0)   # 12/12
    assert r["video_mask_end_time"] == pytest.approx(3.0)     # 36/12


def test_missing_execution_context_raises():
    mb = _import_masks_bridge()
    project, _ = _project(ctx=None)
    with pytest.raises(RuntimeError):
        mb.resolve_mask_times(project)


def test_context_without_mask_keys_raises():
    mb = _import_masks_bridge()
    project, _ = _project(ctx="missing-keys")
    with pytest.raises(RuntimeError):
        mb.resolve_mask_times(project)


def test_zero_fps_guard_yields_zero_seconds():
    mb = _import_masks_bridge()
    project, _ = _project(project_fps=0.0, scene_fps=0.0)
    r = mb.resolve_mask_times(project)
    assert r["video_mask_start_time"] == 0.0
    assert r["video_mask_end_time"] == 0.0
    assert r["audio_mask_start_time"] == 0.0
    assert r["audio_mask_end_time"] == 0.0


# ── Video pixel -> latent mapping ────────────────────────────────────────────


def test_video_span_ltx_on_grid_is_exact():
    mb = _import_masks_bridge()
    # 241 pixel frames -> 31 latents. Boundaries 73 and 145 are on the 8k+1 grid.
    assert mb.resolve_video_mask_span(241, 31, 73, 145, LTX_VIDEO_MAP) == (10, 19)
    assert mb.resolve_video_mask_span(241, 31, 0, 241, LTX_VIDEO_MAP) == (0, 31)
    assert mb.resolve_video_mask_span(241, 31, 0, 1, LTX_VIDEO_MAP) == (0, 1)


def test_video_span_h3_fractional_ratio():
    """H3 is 17k+5 frames <-> 5k+2 latents, i.e. a fractional 3.4:1 ratio."""
    mb = _import_masks_bridge()
    assert mb.resolve_video_mask_span(124, 37, 5, 22, H3_VIDEO_MAP) == (2, 7)
    assert mb.resolve_video_mask_span(124, 37, 0, 124, H3_VIDEO_MAP) == (0, 37)


@pytest.mark.parametrize("frame_count,latent_frames,expected", [
    (22, 7, (2, 7)),     # K=1: a count-only solver admits (4,3), (5,2) and (6,1)
    (39, 12, (2, 12)),   # K=2: a count-only solver admits (4,4) and (5,2)
])
def test_video_span_short_h3_windows_are_unambiguous(frame_count, latent_frames, expected):
    """Regression guard: do NOT replace the VAE map with count-only inference.

    Deriving `(latent_step, latent_offset)` from the pixel/latent counts alone is
    non-unique on short H3 windows, and picking a wrong-but-valid solution
    destroys pre-context silently. `vae.downscale_ratio[0]` is exact here.
    """
    mb = _import_masks_bridge()
    got = mb.resolve_video_mask_span(frame_count, latent_frames, 5, frame_count, H3_VIDEO_MAP)
    assert got == expected


def test_video_pixel_zero_maps_to_zero_despite_h3_map():
    """H3's callable returns 1 for a frame count of 0 or 1, so 0 is special-cased."""
    mb = _import_masks_bridge()
    assert H3_VIDEO_MAP(0) == 1          # the raw core map
    assert mb.video_latent_index(H3_VIDEO_MAP, 0) == 0
    assert mb.video_latent_index(H3_VIDEO_MAP, -5) == 0


def test_video_span_zero_width_is_empty():
    mb = _import_masks_bridge()
    assert mb.resolve_video_mask_span(241, 31, 73, 73, LTX_VIDEO_MAP) == (0, 0)
    assert mb.resolve_video_mask_span(241, 31, 145, 73, LTX_VIDEO_MAP) == (0, 0)


def test_video_span_rejects_latent_from_another_window():
    """A stale/upscaled/wrong-source latent must fail loudly, not map at scale."""
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError, match="not encoded from this window"):
        mb.resolve_video_mask_span(241, 61, 73, 145, LTX_VIDEO_MAP)


def test_video_span_validates_before_zero_width_shortcut():
    """Freeze must not hide a geometry contradiction until the user unfreezes."""
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError):
        mb.resolve_video_mask_span(241, 61, 73, 73, LTX_VIDEO_MAP)


def test_video_span_requires_frame_count():
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError, match="frame_count"):
        mb.resolve_video_mask_span(0, 31, 73, 145, LTX_VIDEO_MAP)


def test_video_downscale_fn_rejects_image_vae():
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError, match="temporal downscale map"):
        mb.video_downscale_fn(types.SimpleNamespace(downscale_ratio=8))
    with pytest.raises(RuntimeError):
        mb.video_downscale_fn(types.SimpleNamespace())


def test_video_downscale_fn_accepts_video_vae():
    mb = _import_masks_bridge()
    assert mb.video_downscale_fn(_video_vae())(241) == 31


# ── Audio geometry ───────────────────────────────────────────────────────────


def test_audio_span_is_by_time_from_the_window_start():
    """Re-derives the old proportional cases: on a window-length axis they agree."""
    mb = _import_masks_bridge()
    # 241 frames at 24 fps, LTX's 25 latents/s, a 252-latent axis.
    assert mb.audio_mask_span(73, 145, 24, 25.0, 252) == (76, 152)       # 76.04 floors, 151.04 ceils
    assert mb.audio_mask_span(0, 241, 24, 25.0, 252, reaches_end=True) == (0, 252)
    # Not reaching the end, time decides: ceil(241 / 24 * 25) = 252 anyway; one frame short:
    assert mb.audio_mask_span(0, 240, 24, 25.0, 252) == (0, 250)
    # H3, 90 frames, 40 latents/s, 151-latent axis.
    assert mb.audio_mask_span(5, 90, 24, 40.0, 151, reaches_end=True) == (8, 151)


def test_audio_span_runs_past_a_padded_axis_only_at_the_end():
    """A grid-padded latent is longer than the window: a proportional map would stretch."""
    mb = _import_masks_bridge()
    # 17 frames at 30 fps on LTX: the window is 14.2 latents, the padded axis 17.
    assert mb.audio_mask_span(4, 12, 30, 25.0, 17) == (3, 10)
    assert mb.audio_mask_span(4, 17, 30, 25.0, 17, reaches_end=True) == (3, 17)


def test_audio_span_conventions():
    mb = _import_masks_bridge()
    # Uniform latent k covers [k, k+1); causal (LTX) [k - 3/4, k + 1/4), latent 0 from 0.
    # 375 ms at 25 latents/s is 9.375 periods.
    assert mb.audio_mask_span(0, 9, 24, 25.0, 40) == (0, 10)                      # ceil(9.375)
    assert mb.audio_mask_span(0, 9, 24, 25.0, 40, "causal") == (0, 11)           # latent 10 starts at 9.25
    assert mb.audio_mask_span(9, 18, 24, 25.0, 40) == (9, 19)
    assert mb.audio_mask_span(9, 18, 24, 25.0, 40, "causal") == (10, 20)         # latent 9 ends at 9.25 < 9.375
    # A window inside latent 0's causal interval [0, 1/4).
    assert mb.audio_mask_span(0, 1, 100, 25.0, 40, "causal") == (0, 1)


def test_audio_span_zero_width_is_empty():
    mb = _import_masks_bridge()
    assert mb.audio_mask_span(73, 73, 24, 25.0, 252) == (0, 0)
    assert mb.audio_mask_span(73, 73, 24, 25.0, 252, reaches_end=True) == (0, 0)


def test_audio_time_axis_ltx_is_height():
    mb = _import_masks_bridge()
    # 241 frames @ 24fps = 10.04s; LTX audio runs at 25 latents/s -> ~251.
    assert mb.detect_audio_time_axis((1, 8, 252, 16), 251) == -2


def test_audio_time_axis_h3_is_width():
    mb = _import_masks_bridge()
    # 90 frames @ 24fps = 3.75s; H3 audio runs at 40 latents/s -> 150.
    assert mb.detect_audio_time_axis((1, 32, 2, 151), 150) == -1


def test_audio_time_axis_short_ltx_clip_still_picks_time():
    """The case a plausible-rate band gets wrong.

    At 9 frames (0.375s) LTX's time axis is 9 and its fixed 16-bin frequency
    axis is *longer*; both imply a plausible latent rate, so a band plus a
    prefer-the-longer-axis tie-break would mask across the spectrum instead.
    Matching the expected length picks correctly.
    """
    mb = _import_masks_bridge()
    assert mb.detect_audio_time_axis((1, 8, 9, 16), 9) == -2


def test_audio_time_axis_tolerates_encode_vs_computed_off_by_one():
    """A real encode ceils through the mel pipeline; the computed length rounds."""
    mb = _import_masks_bridge()
    assert mb.detect_audio_time_axis((1, 8, 252, 16), 251) == -2
    assert mb.detect_audio_time_axis((1, 8, 250, 16), 251) == -2


def test_audio_time_axis_no_match_raises():
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError, match="neither"):
        mb.detect_audio_time_axis((1, 8, 252, 16), 40)


def test_audio_time_axis_prefers_the_nearer_axis_when_both_are_in_tolerance():
    """Reported 2026-08-20: a 17-frame LTX upscale window blocked generation.

    Expected 18 audio steps, actual axes (17, 16) — the fixed 16-bin frequency
    axis also falls inside the tolerance. The nearer axis is still the right
    one, so this must resolve rather than refuse.
    """
    mb = _import_masks_bridge()
    assert mb.detect_audio_time_axis((1, 8, 17, 16), 18) == -2


def test_audio_time_axis_only_a_genuine_tie_raises():
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError, match="equally close"):
        mb.detect_audio_time_axis((1, 8, 100, 100), 100)
    # equidistant either side of the expected length is also unresolvable
    with pytest.raises(RuntimeError, match="equally close"):
        mb.detect_audio_time_axis((1, 8, 17, 19), 18)


def test_audio_time_axis_zero_expected_raises():
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError):
        mb.detect_audio_time_axis((1, 8, 252, 16), 0)


def test_audio_rate_read_from_vae():
    mb = _import_masks_bridge()
    assert mb.audio_latents_per_second(_audio_vae(25.0)) == pytest.approx(25.0)
    assert mb.audio_latents_per_second(_audio_vae(40.0)) == pytest.approx(40.0)


def test_audio_rate_missing_raises():
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError, match="latents_per_second"):
        mb.audio_latents_per_second(types.SimpleNamespace())


# ── Latent shape guards ──────────────────────────────────────────────────────


def test_latent_shape_rejects_nested_av_latent():
    """A joint AV latent reports tensors[0].shape and max ndim, so it would pass
    a plain rank check and silently mask the wrong stream."""
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError, match="Separate AV Latent"):
        mb._latent_shape(_latent((1, 128, 31, 22, 39), nested=True), "video_latent", 5)


def test_latent_shape_rejects_wrong_rank():
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError, match="rank-5"):
        mb._latent_shape(_latent((1, 8, 252, 16)), "video_latent", 5)
    with pytest.raises(RuntimeError, match="rank-4"):
        mb._latent_shape(_latent((1, 128, 31, 22, 39)), "audio_latent", 4)


def test_latent_shape_rejects_non_latent():
    mb = _import_masks_bridge()
    with pytest.raises(RuntimeError, match="not a LATENT"):
        mb._latent_shape({"nope": 1}, "video_latent", 5)


def test_latent_shape_passes_through_none():
    mb = _import_masks_bridge()
    assert mb._latent_shape(None, "video_latent", 5) is None


# ── Node contract ────────────────────────────────────────────────────────────


def test_node_contract():
    mb = _import_masks_bridge()
    node_cls = mb.SonderMasksBridge
    assert node_cls.RETURN_TYPES == ("FLOAT", "FLOAT", "FLOAT", "FLOAT", "MASK", "MASK")
    assert node_cls.RETURN_NAMES == (
        "video_mask_start_time", "video_mask_end_time",
        "audio_mask_start_time", "audio_mask_end_time",
        "video_mask", "audio_mask",
    )
    # A missing tooltip is the classic silent omission on an appended output.
    assert len(node_cls.OUTPUT_TOOLTIPS) == len(node_cls.RETURN_TYPES)
    assert len(node_cls.RETURN_NAMES) == len(node_cls.RETURN_TYPES)

    spec = node_cls.INPUT_TYPES()
    required = spec["required"]
    assert "project" in required
    assert "edit_video" in required and "edit_audio" in required
    assert required["edit_video"][1]["default"] is True
    assert required["edit_audio"][1]["default"] is True

    optional = spec["optional"]
    assert optional["video_latent"][0] == "LATENT"
    assert optional["audio_latent"][0] == "LATENT"
    assert optional["video_vae"][0] == "VAE"
    assert optional["audio_vae"][0] == "VAE"
    assert spec["hidden"]["unique_id"] == "UNIQUE_ID"


def test_resolve_mask_times_reports_frame_count_padding():
    mb = _import_masks_bridge()
    project, _ = _project()

    # An execution context written before the key existed must yield 0, not raise.
    assert mb.resolve_mask_times(project, True, True)["frame_count_padding"] == 0

    project._execution_context["frame_count_padding"] = 9
    assert mb.resolve_mask_times(project, True, True)["frame_count_padding"] == 9

    # Negative or unparseable values floor at 0 rather than propagating.
    project._execution_context["frame_count_padding"] = -4
    assert mb.resolve_mask_times(project, True, True)["frame_count_padding"] == 0
    project._execution_context["frame_count_padding"] = "not-a-number"
    assert mb.resolve_mask_times(project, True, True)["frame_count_padding"] == 0


# ── Tensor emission (needs torch) ────────────────────────────────────────────


def test_execute_returns_six_outputs_and_records_provenance():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project, _ = _project()
    node = mb.SonderMasksBridge()
    out = node.execute(project, edit_video=True, edit_audio=False)
    assert len(out) == 6
    assert out[:4] == (
        pytest.approx(0.5), pytest.approx(1.5), pytest.approx(0.5), pytest.approx(0.5))
    # No latents wired -> keep-everything fallbacks.
    assert tuple(out[4].shape) == (1, 1, 1) and float(out[4].max()) == 0.0
    assert tuple(out[5].shape) == (1, 1, 1) and float(out[5].max()) == 0.0

    prov = project._execution_context.get("masks_bridge")
    assert prov is not None
    assert prov["edit_video"] is True
    assert prov["edit_audio"] is False
    assert prov["video_mask"] == [pytest.approx(0.5), pytest.approx(1.5)]
    assert prov["audio_mask"] == [pytest.approx(0.5), pytest.approx(0.5)]
    assert prov["video_latent_mask"] is None
    assert prov["audio_latent_mask"] is None


def test_execute_compiles_hard_masks_for_both_streams():
    torch = pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project, _ = _project(mask_start=73, mask_end=145, frame_count=241)
    node = mb.SonderMasksBridge()
    out = node.execute(
        project,
        video_latent=_latent((1, 128, 31, 22, 39)), video_vae=_video_vae(),
        audio_latent=_latent((1, 8, 252, 16)), audio_vae=_audio_vae(25.0),
    )
    video_mask, audio_mask = out[4], out[5]

    # Video: a batch, one entry per latent frame, hard 0/1 over latents 10..18.
    assert tuple(video_mask.shape) == (31, 1, 1)
    assert set(video_mask.unique().tolist()) == {0.0, 1.0}
    assert bool(video_mask[10:19].all()) and float(video_mask[:10].sum()) == 0.0
    assert float(video_mask[19:].sum()) == 0.0

    # Audio: a SINGLE image shaped to the latent's last two axes, time on -2.
    assert tuple(audio_mask.shape) == (1, 252, 16)
    assert set(audio_mask.unique().tolist()) == {0.0, 1.0}
    assert bool(audio_mask[0, 76:152, :].all())
    assert float(audio_mask[0, :76, :].sum()) == 0.0
    assert float(audio_mask[0, 152:, :].sum()) == 0.0

    prov = project._execution_context["masks_bridge"]
    assert prov["video_latent_mask"]["latent_span"] == [10, 19]
    assert prov["audio_latent_mask"]["latent_span"] == [76, 152]
    assert prov["audio_latent_mask"]["time_axis"] == -2


def test_execute_compiles_h3_audio_on_the_width_axis():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    # 90 frames @ 24fps = 3.75s; H3 audio at 40/s -> 150 steps.
    project, _ = _project(mask_start=5, mask_end=90, frame_count=90)
    node = mb.SonderMasksBridge()
    out = node.execute(
        project,
        video_latent=_latent((1, 24, 27, 48, 84)), video_vae=_video_vae(H3_VIDEO_MAP),
        audio_latent=_latent((1, 32, 2, 151)), audio_vae=_audio_vae(40.0),
    )
    audio_mask = out[5]
    assert tuple(audio_mask.shape) == (1, 2, 151)
    lo = (5 * 151) // 90
    assert bool(audio_mask[0, :, lo:].all())
    assert float(audio_mask[0, :, :lo].sum()) == 0.0

    prov = project._execution_context["masks_bridge"]
    assert prov["audio_latent_mask"]["time_axis"] == -1
    assert prov["video_latent_mask"]["latent_frames"] == 27   # H3_VIDEO_MAP(90)


def test_execute_freeze_emits_all_zero_mask_at_real_shape():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project, _ = _project(mask_start=73, mask_end=145, frame_count=241)
    node = mb.SonderMasksBridge()
    out = node.execute(
        project, edit_video=False,
        video_latent=_latent((1, 128, 31, 22, 39)), video_vae=_video_vae(),
    )
    video_mask = out[4]
    assert tuple(video_mask.shape) == (31, 1, 1)   # real shape, not the 1x1 fallback
    assert float(video_mask.sum()) == 0.0


def test_frozen_audio_never_resolves_the_time_axis():
    """Reported 2026-08-20: an upscale pass with audio frozen still hard-failed.

    A frozen channel's mask is all-zeros whichever axis is time, so a short clip
    whose frequency axis collides with its time axis must not block the render.
    Here BOTH axes are equally close to the expected length — unresolvable — and
    it must still succeed because no audio is being generated.
    """
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project, _ = _project(mask_start=73, mask_end=145, frame_count=241)
    node = mb.SonderMasksBridge()
    out = node.execute(
        project, edit_audio=False,
        audio_latent=_latent((1, 8, 100, 100)), audio_vae=_audio_vae(25.0),
    )
    audio_mask = out[5]
    assert tuple(audio_mask.shape) == (1, 100, 100)   # real shape, not the 1x1 fallback
    assert float(audio_mask.sum()) == 0.0
    prov = project._execution_context["masks_bridge"]["audio_latent_mask"]
    assert prov["frozen"] is True and prov["time_axis"] is None


def test_frozen_audio_with_short_colliding_axes_succeeds():
    """The exact reported shape: axes (17, 16) against an expected 18."""
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project, _ = _project(mask_start=0, mask_end=0, frame_count=17)
    node = mb.SonderMasksBridge()
    out = node.execute(
        project, edit_audio=False,
        audio_latent=_latent((1, 8, 17, 16)), audio_vae=_audio_vae(25.0),
    )
    assert tuple(out[5].shape) == (1, 17, 16)
    assert float(out[5].sum()) == 0.0


def test_execute_partial_wiring_falls_back_to_keep_everything():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project, _ = _project(mask_start=73, mask_end=145, frame_count=241)
    node = mb.SonderMasksBridge()
    out = node.execute(project, video_latent=_latent((1, 128, 31, 22, 39)))  # no vae
    assert tuple(out[4].shape) == (1, 1, 1)
    assert float(out[4].sum()) == 0.0


def test_execute_wired_latent_without_frame_count_raises():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project, _ = _project(mask_start=73, mask_end=145, frame_count=0)
    node = mb.SonderMasksBridge()
    with pytest.raises(RuntimeError, match="frame_count"):
        node.execute(project, video_latent=_latent((1, 128, 31, 22, 39)),
                     video_vae=_video_vae())


def test_mask_provenance_is_json_serializable():
    """_public_execution_context json-clones every non-underscore ctx key."""
    import json
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project, _ = _project(mask_start=73, mask_end=145, frame_count=241)
    node = mb.SonderMasksBridge()
    node.execute(
        project,
        video_latent=_latent((1, 128, 31, 22, 39)), video_vae=_video_vae(),
        audio_latent=_latent((1, 8, 252, 16)), audio_vae=_audio_vae(25.0),
    )
    ctx = project._execution_context
    json.dumps(ctx["masks_bridge"])
    json.dumps(ctx["masks_bridge_by_node"])


def test_two_bridges_do_not_clobber_each_others_provenance():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project, _ = _project()
    mb.SonderMasksBridge().execute(project, edit_video=True, unique_id="11")
    mb.SonderMasksBridge().execute(project, edit_video=False, unique_id="22")
    by_node = project._execution_context["masks_bridge_by_node"]
    assert by_node["11"]["edit_video"] is True
    assert by_node["22"]["edit_video"] is False


# ── Parity with core's real resampler ────────────────────────────────────────


def _core_reshape_mask():
    """Core's REAL `reshape_mask`, loaded from the installed source, or skip.

    A locally re-implemented copy would be a mirror, not a parity test: it keeps
    passing when core's behaviour changes, which is exactly the failure mode
    recorded for `reference_bridge_shape.js`. Importing `comfy.utils` outright
    needs ComfyUI's own venv (`comfy_aimdo`), so the two functions are exec'd
    straight out of the installed file instead — the code under test is core's
    current source, so a change there changes this test. Set SONDER_COMFY_PATH
    to point at another checkout.
    """
    torch = pytest.importorskip("torch")
    roots = [os.environ.get("SONDER_COMFY_PATH"),
             r"D:\Stability Matrix\Stability Data\Packages\ComfyUI Sonder Test"]
    path = next((os.path.join(r, "comfy", "utils.py") for r in roots
                 if r and os.path.isfile(os.path.join(r, "comfy", "utils.py"))), None)
    if path is None:
        pytest.skip("ComfyUI source not found; set SONDER_COMFY_PATH to run the parity check")
    src = Path(path).read_text(encoding="utf-8").splitlines()

    def grab(name):
        start = next((i for i, line in enumerate(src)
                      if line.startswith("def {}(".format(name))), None)
        if start is None:
            pytest.fail("core no longer defines {} in comfy/utils.py".format(name))
        end = next((i for i in range(start + 1, len(src))
                    if src[i] and not src[i][0].isspace()), len(src))
        return "\n".join(src[start:end])

    namespace = {"torch": torch}
    for name in ("repeat_to_batch_size", "reshape_mask"):
        exec(compile(grab(name), path, "exec"), namespace)
    return namespace["reshape_mask"]


def _set_latent_noise_mask(mask):
    """nodes.py:1567 — what SetLatentNoiseMask does to a MASK before sampling."""
    return mask.reshape((-1, 1, mask.shape[-2], mask.shape[-1]))


def test_masks_survive_core_reshape_exactly():
    """The compiled masks must reach the sampler unchanged, for both streams."""
    torch = pytest.importorskip("torch")
    reshape_mask = _core_reshape_mask()
    mb = _import_masks_bridge()
    project, _ = _project(mask_start=73, mask_end=145, frame_count=241)
    out = mb.SonderMasksBridge().execute(
        project,
        video_latent=_latent((1, 128, 31, 22, 39)), video_vae=_video_vae(),
        audio_latent=_latent((1, 8, 252, 16)), audio_vae=_audio_vae(25.0),
    )

    video = reshape_mask(_set_latent_noise_mask(out[4]), (1, 128, 31, 22, 39))
    want_v = torch.zeros(31)
    want_v[10:19] = 1.0
    assert torch.equal(video[0, 0, :, 0, 0], want_v)
    assert float(video.min()) == 0.0 and float(video.max()) == 1.0

    audio = reshape_mask(_set_latent_noise_mask(out[5]), (1, 8, 252, 16))
    want_a = torch.zeros(252)
    want_a[76:152] = 1.0
    assert torch.equal(audio[0, 0, :, 0], want_a)


def test_pixel_count_video_mask_does_not_survive_core_reshape():
    """Pins why the video mask is emitted at LATENT count, not pixel count.

    A 241-frame batch trilinearly resamples onto 31 latents off by one at the
    head: for the 73..145 window, latent 9 reads 1.0 when it must be 0.0,
    eating a latent of pre-context. If a future change "simplifies" back to
    pixel frames, this fails.
    """
    torch = pytest.importorskip("torch")
    reshape_mask = _core_reshape_mask()

    pixel_mask = torch.zeros((241, 1, 1))
    pixel_mask[73:145] = 1.0
    got = reshape_mask(_set_latent_noise_mask(pixel_mask), (1, 128, 31, 22, 39))[0, 0, :, 0, 0]
    want = torch.zeros(31)
    want[10:19] = 1.0
    assert not torch.equal(got, want)
    # the pre-context latent the resample wrongly turns on
    assert float(got[9]) == 1.0 and float(want[9]) == 0.0


def test_frozen_channel_provenance_records_pinned_padding():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    # A window that reaches the padded tail, so the editing channel genuinely regenerates
    # the pad and Freeze is the only thing pinning it. See
    # test_edit_channel_still_pins_padding_without_any_freeze for the ordinary case where
    # post-context leaves the pad outside an editing channel's mask too.
    project, _ = _project(mask_start=12, mask_end=241, frame_count=241)
    project._execution_context["frame_count_padding"] = 9
    node = mb.SonderMasksBridge()

    node.execute(project, edit_video=True, edit_audio=False)
    prov = project._execution_context["masks_bridge"]

    assert prov["frame_count_padding"] == 9
    # Freeze collapses the window to zero width, so it can never reach the tail.
    assert prov["audio_pins_padding"] is True
    assert prov["video_pins_padding"] is False
    # The real guarantee: this payload flows into every take's generation_params via
    # _public_execution_context, so it must stay JSON-safe - never a tensor.
    assert json.loads(json.dumps(prov))["frame_count_padding"] == 9

    # No padding means nothing is pinned, however the channels are set.
    project._execution_context["frame_count_padding"] = 0
    node.execute(project, edit_video=False, edit_audio=False)
    prov = project._execution_context["masks_bridge"]
    assert prov["frame_count_padding"] == 0
    assert prov["video_pins_padding"] is False
    assert prov["audio_pins_padding"] is False
    json.dumps(prov)


def test_edit_channel_still_pins_padding_without_any_freeze():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    # Both channels editing, but the window ends before the padded tensor - the ordinary
    # shape of any render carrying post-context. Nothing is frozen, and both channels
    # still keep the fabricated tail, so provenance must not read the Edit/Freeze switch.
    project, _ = _project(mask_start=39, mask_end=141, frame_count=175)
    project._execution_context["frame_count_padding"] = 2
    node = mb.SonderMasksBridge()

    node.execute(project, edit_video=True, edit_audio=True)
    prov = project._execution_context["masks_bridge"]

    assert prov["edit_video"] is True and prov["edit_audio"] is True
    assert prov["video_pins_padding"] is True
    assert prov["audio_pins_padding"] is True

    # A window that does reach the tail regenerates the pad on both channels.
    reaching, _ = _project(mask_start=39, mask_end=141, frame_count=141)
    reaching._execution_context["frame_count_padding"] = 2
    node.execute(reaching, edit_video=True, edit_audio=True)
    prov = reaching._execution_context["masks_bridge"]
    assert prov["video_pins_padding"] is False
    assert prov["audio_pins_padding"] is False

    # Freeze remains the zero-width special case, pinning regardless of where the mask ends.
    node.execute(reaching, edit_video=False, edit_audio=True)
    prov = reaching._execution_context["masks_bridge"]
    assert prov["video_pins_padding"] is True
    assert prov["audio_pins_padding"] is False


# ── The Editor's audio grid (Audio Grid Padding plan, Phase 2) ───────────────

class MiniMaxH3AudioVAE:  # the class name is what identifies H3's layout
    latents_per_second = 40.0


def _ltx_audio_vae(**attrs):
    inner = types.SimpleNamespace(
        latents_per_second=25.0, latent_frequency_bins=16,
        autoencoder=types.SimpleNamespace(causality_axis="CausalityAxis.HEIGHT"))  # the real LTX value
    return types.SimpleNamespace(first_stage_model=inner, **attrs)


def _h3_audio_vae(**attrs):
    return types.SimpleNamespace(first_stage_model=MiniMaxH3AudioVAE(), **attrs)


def _grid_project(*, mask_start, mask_end, frame_count, sample_count, sample_rate, project_fps=24.0,
                  constraint=None):
    project, scene = _project(mask_start=mask_start, mask_end=mask_end, frame_count=frame_count,
                              project_fps=project_fps)
    project._execution_context.update({
        "audio_sample_rate": sample_rate, "audio_sample_count": sample_count,
        "audio_constraint": constraint or {"rate": sample_rate, "block": 4096},
        "audio_grid_padding_samples": 1,
    })
    return project


def test_known_layouts_come_from_the_vae_not_the_lengths():
    mb = _import_masks_bridge()
    assert mb.known_audio_layout(_ltx_audio_vae(), (1, 8, 17, 16)) == (-2, "causal", "ltx")
    assert mb.known_audio_layout(_h3_audio_vae(), (1, 32, 2, 151)) == (-1, "uniform", "minimax_h3")
    assert mb.known_audio_layout(_audio_vae(25.0), (1, 8, 17, 16)) is None       # unknown: length detection
    assert mb.known_audio_layout(_ltx_audio_vae(), (1, 8, 17, 12)) is None       # bins do not match


def test_a_padded_short_ltx_window_masks_time_not_frequency():
    """Audit 2, blocker 1: 17 frames at 30 fps, expectations 14 (window) and 16 (padded).

    The padded latent is `[1, 8, 17, 16]`: by length, 16 matches the frequency axis.
    """
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project = _grid_project(mask_start=4, mask_end=17, frame_count=17, project_fps=30.0,
                            sample_count=28672, sample_rate=44100)
    out = mb.SonderMasksBridge().execute(project, audio_latent=_latent((1, 8, 17, 16)),
                                         audio_vae=_ltx_audio_vae(audio_sample_rate=44100, downscale_ratio=4096,
                                                                  crop_input=True))
    prov = project._execution_context["masks_bridge"]["audio_latent_mask"]
    assert prov["time_axis"] == -2 and prov["axis_source"] == "layout"
    assert prov["time_convention"] == "causal"
    assert prov["expected_length"] == 14 and prov["expected_padded_length"] == 16
    assert prov["matched"] == ["padded"]
    # Reaches the end, so the whole padded axis is regenerated.
    assert prov["latent_span"][1] == 17
    assert tuple(out[5].shape) == (1, 17, 16)


def test_a_window_length_latent_still_passes_when_the_editor_padded_its_audio():
    """A graph that encodes some other window-length AUDIO is not refused (no new refusal path)."""
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project = _grid_project(mask_start=73, mask_end=145, frame_count=241, sample_count=446464, sample_rate=44100)
    mb.SonderMasksBridge().execute(project, audio_latent=_latent((1, 8, 249, 16)), audio_vae=_ltx_audio_vae())  # window 251, padded 253
    prov = project._execution_context["masks_bridge"]["audio_latent_mask"]
    assert prov["matched"] == ["window"] and prov["time_axis"] == -2


def test_an_unknown_layout_matches_the_padded_length_before_refusing():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    # 17 frames at 24 fps: window 18 latents, padded 21 (4096-sample blocks).
    project = _grid_project(mask_start=4, mask_end=12, frame_count=17, sample_count=36864, sample_rate=44100)
    mb.SonderMasksBridge().execute(project, audio_latent=_latent((1, 8, 21, 40)), audio_vae=_audio_vae(25.0))
    prov = project._execution_context["masks_bridge"]["audio_latent_mask"]
    assert prov["time_axis"] == -2 and prov["axis_source"] == "length" and prov["matched"] == ["padded"]


def test_an_edit_reaching_the_end_extends_the_audio_end_time_over_the_grid_tail():
    mb = _import_masks_bridge()
    project = _grid_project(mask_start=12, mask_end=241, frame_count=241, sample_count=446464, sample_rate=44100)
    result = mb.resolve_mask_times(project, edit_audio=True)
    assert result["audio_mask_end_time"] == pytest.approx(446464 / 44100)
    assert result["video_mask_end_time"] == pytest.approx(241 / 24)
    # Not reaching the end, or frozen, or without a grid: unchanged.
    short = _grid_project(mask_start=12, mask_end=200, frame_count=241, sample_count=446464, sample_rate=44100)
    assert mb.resolve_mask_times(short)["audio_mask_end_time"] == pytest.approx(200 / 24)
    assert mb.resolve_mask_times(project, edit_audio=False)["audio_mask_end_time"] == pytest.approx(12 / 24)
    plain, _ = _project(mask_start=12, mask_end=241, frame_count=241)
    assert mb.resolve_mask_times(plain)["audio_mask_end_time"] == pytest.approx(241 / 24)


@pytest.mark.parametrize("vae,declared,status", [
    (dict(audio_sample_rate=44100, downscale_ratio=4096, crop_input=True), {"rate": 44100, "block": 4096}, "match"),
    (dict(audio_sample_rate=32000, downscale_ratio=800, crop_input=True), {"rate": 44100, "block": 4096}, "mismatch"),
    (dict(audio_sample_rate=44100, downscale_ratio=512.0, crop_input=False), {"rate": 44100, "block": 4096}, "no_crop"),
    (dict(audio_sample_rate=44100, downscale_ratio=4096, crop_input=True), None, "no_grid"),
    (dict(), {"rate": 44100, "block": 4096}, "unknown"),
])
def test_grid_check_statuses(vae, declared, status):
    mb = _import_masks_bridge()
    assert mb.audio_grid_check(declared, types.SimpleNamespace(**vae))["status"] == status
    assert mb.audio_grid_check(declared, None) is None


def test_grid_check_runs_on_freeze_warns_on_mismatch_and_never_raises(caplog):
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    project = _grid_project(mask_start=73, mask_end=145, frame_count=241, sample_count=446464, sample_rate=44100)
    vae = _h3_audio_vae(audio_sample_rate=32000, downscale_ratio=800, crop_input=True)
    with caplog.at_level("WARNING"):
        mb.SonderMasksBridge().execute(project, edit_audio=False, audio_latent=_latent((1, 32, 2, 402)), audio_vae=vae)
    payload = project._execution_context["masks_bridge"]
    assert payload["audio_grid_check"]["status"] == "mismatch"
    assert payload["audio_latent_mask"]["frozen"] is True
    assert payload["audio_grid_padding_pinned"] is True
    assert any("does not match the wired audio VAE" in r.getMessage() for r in caplog.records)


def test_repeated_runs_publish_identical_provenance():
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    payloads = []
    for _ in range(2):
        project = _grid_project(mask_start=4, mask_end=17, frame_count=17, project_fps=30.0,
                                sample_count=28672, sample_rate=44100)
        mb.SonderMasksBridge().execute(project, audio_latent=_latent((1, 8, 17, 16)), audio_vae=_ltx_audio_vae(),
                                       unique_id="7")
        payloads.append(json.dumps(project._execution_context["masks_bridge"], sort_keys=True))
    assert payloads[0] == payloads[1]


@pytest.mark.parametrize("frames,fps,shape,vae,expected,padded,sample_count", [
    # Shapes the real VAEs encode from the Editor's grid audio (Phase 2 audit probe
    # `real_vae_probe.json`, real weights). LTX: 44100 Hz, blocks of 4096; H3: 32000 Hz, 800.
    (121, 24, (1, 8, 128, 16), "ltx", 126, 128, 225280),   # 55 blocks
    (17, 30, (1, 8, 17, 16), "ltx", 14, 16, 28672),        # 7 blocks; the real axis is one past padded
    (121, 24, (1, 32, 2, 202), "h3", 202, 202, 161600),    # 202 blocks
])
def test_real_grid_latent_shapes_resolve_on_the_time_axis(frames, fps, shape, vae, expected, padded,
                                                          sample_count):
    pytest.importorskip("torch")
    mb = _import_masks_bridge()
    sample_rate, block = (44100, 4096) if vae == "ltx" else (32000, 800)
    project = _grid_project(mask_start=0, mask_end=frames, frame_count=frames, project_fps=float(fps),
                            sample_count=sample_count, sample_rate=sample_rate,
                            constraint={"rate": sample_rate, "block": block})
    audio_vae = _ltx_audio_vae() if vae == "ltx" else _h3_audio_vae()
    mb.SonderMasksBridge().execute(project, audio_latent=_latent(shape), audio_vae=audio_vae)
    prov = project._execution_context["masks_bridge"]["audio_latent_mask"]
    axis = -2 if vae == "ltx" else -1
    assert prov["time_axis"] == axis and prov["axis_source"] == "layout"
    assert prov["expected_length"] == expected and prov["expected_padded_length"] == padded
    assert prov["latent_span"] == [0, shape[axis]]  # reaches the end: the whole padded axis


# ── The kjnodes LTX masker on grid audio (Phase 2 audit F4) ─────────────────

_KJ_SCRIPT = r"""
import importlib.util, json, os, sys, types
comfy_root, kj_root, cases = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
sys.argv = [sys.argv[0], "--cpu"]
sys.path.insert(0, comfy_root)
import torch
import server
# The module registers preview routes at import; no server runs here.
route = lambda *a, **k: (lambda f: f)
server.PromptServer.instance = types.SimpleNamespace(
    routes=types.SimpleNamespace(post=route, get=route), send_sync=lambda *a, **k: None)
# By file path: ComfyUI's own `nodes.py` shadows the kjnodes `nodes` package.
spec = importlib.util.spec_from_file_location("kj_ltxv_nodes", os.path.join(kj_root, "nodes", "ltxv_nodes.py"))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
LTXVAudioVideoMask = module.LTXVAudioVideoMask
out = []
for case in cases:
    for mode in ("partial", "truncate", "pad"):
        latent = {"samples": torch.ones(1, 8, case["T"], 16)}
        result = LTXVAudioVideoMask.execute(24.0, 0.0, 0.0, case["start"], case["end"], max_length=mode,
                                            audio_latent=latent)
        audio = result.args[1] if hasattr(result, "args") else result[1]
        mask = audio["noise_mask"]
        out.append({"T": case["T"], "mode": mode, "length": int(audio["samples"].shape[2]),
                    "masked_to_end": bool(mask[:, :, case["first"]:].min() == 1.0),
                    "added_zero": bool(audio["samples"][:, :, case["T"]:].abs().sum() == 0)})
print("KJ_RESULT " + json.dumps(out))
"""


def _comfy_root():
    candidates = []
    if os.environ.get("SONDER_COMFYUI_ROOT"):
        candidates.append(Path(os.environ["SONDER_COMFYUI_ROOT"]))
    candidates.extend(Path(sys.executable).resolve().parents[:3])
    for root in candidates:
        if (root / "comfy" / "sd.py").is_file() and (root / "comfy_extras" / "nodes_lt.py").is_file():
            return root
    return None


def test_kjnodes_ltx_masker_never_cuts_grid_audio_and_pad_adds_at_most_one_token():
    """Each mode on the bridge's grid end time, with latent lengths the real LTX audio VAE
    gives for whole-block audio (the Phase 2 audit's `kj_sweep` over real weights).
    `partial` and `truncate` keep every latent and mask through the end; `pad` may add one
    zeroed, masked token (stock already overshoots by one there)."""
    pytest.importorskip("torch")
    comfy_root = _comfy_root()
    kj_root = comfy_root / "custom_nodes" / "comfyui-kjnodes" if comfy_root else None
    if kj_root is None or not (kj_root / "nodes" / "ltxv_nodes.py").is_file():
        pytest.skip("needs a ComfyUI install with comfyui-kjnodes (SONDER_COMFYUI_ROOT or the test venv)")
    mb = _import_masks_bridge()
    cases = []
    # (samples, real latent length): 7, 30, 31, 55, 56 and 119 blocks of 4096 at 44100 Hz.
    for samples, latents in ((28672, 17), (122880, 70), (126976, 72), (225280, 128), (229376, 131), (487424, 277)):
        project = _grid_project(mask_start=12, mask_end=24, frame_count=24, sample_count=samples,
                                sample_rate=44100, constraint={"rate": 44100, "block": 4096})
        times = mb.resolve_mask_times(project)
        assert times["audio_mask_end_time"] == pytest.approx(samples / 44100)
        cases.append({"T": latents, "start": times["audio_mask_start_time"],
                      "end": times["audio_mask_end_time"], "first": round(times["audio_mask_start_time"] * 25)})
    completed = subprocess.run([sys.executable, "-c", _KJ_SCRIPT, str(comfy_root), str(kj_root), json.dumps(cases)],
                               cwd=str(comfy_root), capture_output=True, text=True, timeout=600,
                               env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    line = next((item for item in completed.stdout.splitlines() if item.startswith("KJ_RESULT ")), None)
    assert line is not None, completed.stderr[-4000:]
    rows = json.loads(line[len("KJ_RESULT "):])
    assert len(rows) == 18
    for row in rows:
        assert row["masked_to_end"], row
        if row["mode"] == "pad":
            assert row["length"] in (row["T"], row["T"] + 1) and row["added_zero"], row
        else:
            assert row["length"] == row["T"], row
    assert any(row["mode"] == "pad" and row["length"] == row["T"] + 1 for row in rows)

