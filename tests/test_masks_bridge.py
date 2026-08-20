"""Tests for SonderMasksBridge per-channel mask-time gating and mask compiling.

The bridge's resolution core is pure-Python (no relative imports), so it imports
through the fake package like the other bridge tests. Only the tensor emitters
need torch, so those tests guard with `pytest.importorskip("torch")` per-test
rather than at module level — the span/geometry tests below are the bulk of the
coverage and must still run in a bare environment.
"""

import importlib
import math
import os
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


def test_audio_span_is_uniform_and_fps_free():
    mb = _import_masks_bridge()
    assert mb.resolve_audio_mask_span(241, 252, 73, 145) == (76, 152)
    assert mb.resolve_audio_mask_span(241, 252, 0, 241) == (0, 252)


def test_audio_span_zero_width_is_empty():
    mb = _import_masks_bridge()
    assert mb.resolve_audio_mask_span(241, 252, 73, 73) == (0, 0)


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
