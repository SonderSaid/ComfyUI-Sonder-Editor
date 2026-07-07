import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server.color_correction as cc
import server.media_helpers as media_helpers


# --- Fixtures -------------------------------------------------------------


def make_clip(n_frames=32, height=36, width=64, *, dark=False, seed=7):
    """Synthetic float32 clip (N,H,W,3) in 0..1: gradient plus deterministic noise."""
    rng = np.random.default_rng(seed)
    ramp_x = np.linspace(0.05, 0.95, width, dtype=np.float32)
    ramp_y = np.linspace(0.1, 0.9, height, dtype=np.float32)
    base = np.stack(
        [
            np.outer(ramp_y, ramp_x),
            np.outer(ramp_y[::-1], ramp_x),
            np.outer(ramp_y, ramp_x[::-1]),
        ],
        axis=-1,
    )
    frames = np.repeat(base[None, ...], n_frames, axis=0)
    frames = frames + rng.normal(0.0, 0.02, frames.shape).astype(np.float32)
    frames = frames.clip(0.0, 1.0).astype(np.float32)
    if dark:
        frames = frames * 0.45
    return frames


def known_affine():
    """A ground-truth affine inside the drift clamps (0..255-domain bias)."""
    matrix = np.eye(3) + np.array(
        [[0.0, 0.03, -0.02], [0.01, 0.0, 0.02], [-0.03, 0.01, 0.0]]
    )
    bias = np.array([-1.5, 0.8, -0.6])
    return np.concatenate([matrix, bias.reshape(3, 1)], axis=1)


def apply_affine_to_clip01(clip01, affine):
    """Apply a 0..255-domain affine to a 0..1 float clip, returning 0..1 floats."""
    flat = clip01.reshape(-1, 3).astype(np.float64) * 255.0
    out = flat @ affine[:, :3].T + affine[:, 3]
    return (out / 255.0).reshape(clip01.shape).astype(np.float32)


def ltx_like_drift(clip01):
    """Luminance-dependent darkening modeled on the measured LTX 2.3 signature.

    Bias scales linearly from -6/255 at luma 0 to 0 at luma 0.5 — on dark
    content (luma < 0.5) this operator is exactly affine in RGB, matching how
    the real drift behaved on the validation chain's shadow-heavy scene.
    """
    luma = (
        0.2126 * clip01[..., 0] + 0.7152 * clip01[..., 1] + 0.0722 * clip01[..., 2]
    )
    scale = np.clip(1.0 - luma / 0.5, 0.0, 1.0)
    delta = (-6.0 / 255.0) * scale
    return (clip01 + delta[..., None]).clip(0.0, 1.0).astype(np.float32)


def make_stash_context(clip01, *, mask_start=12, mask_end=24, step=1):
    n = clip01.shape[0]
    stash = cc.build_context_reference_stash(
        clip01,
        frame_count=n,
        source_frame_count=n,
        mask_start_frame=mask_start,
        mask_end_frame=mask_end,
        frame_constraint={"step": step} if step > 1 else None,
    )
    assert stash is not None
    return {cc.DRIFT_STASH_CONTEXT_KEY: stash}, stash


# --- Generic core ----------------------------------------------------------


def test_fit_recovers_known_affine():
    clip = make_clip(n_frames=6)
    src = clip.reshape(-1, 3) * 255.0
    tgt = apply_affine_to_clip01(clip, known_affine()).reshape(-1, 3) * 255.0

    # Near-zero ridge isolates the least-squares math for exact recovery; the
    # default ridge's (deliberate) shrink-toward-identity behavior is covered
    # by the functional residual tests below.
    affine, stats = cc.fit_affine_color_transform(src, tgt, ridge_lambda=1e-9)

    assert np.allclose(affine[:, :3], known_affine()[:, :3], atol=1e-3)
    assert np.allclose(affine[:, 3], known_affine()[:, 3], atol=1e-2)
    assert stats["mae_after"] < 0.05
    assert stats["mae_after"] < stats["mae_before"]

    # Default ridge stays functionally accurate on the same data even though
    # the parameter split shifts slightly toward identity.
    _, default_stats = cc.fit_affine_color_transform(src, tgt)
    assert default_stats["mae_after"] < 0.1
    assert default_stats["mae_after"] < default_stats["mae_before"]


def test_fit_cancels_ltx_like_drift_on_held_out_frame():
    clip = make_clip(n_frames=8, dark=True)
    drifted = ltx_like_drift(clip)
    train = slice(0, 6)
    held_out = 7

    affine, _ = cc.fit_affine_color_transform(
        drifted[train].reshape(-1, 3) * 255.0, clip[train].reshape(-1, 3) * 255.0
    )
    src = drifted[held_out].reshape(-1, 3) * 255.0
    ref = clip[held_out].reshape(-1, 3) * 255.0
    corrected = src @ affine[:, :3].T + affine[:, 3]

    mae_before = np.mean(np.abs(src - ref))
    mae_after = np.mean(np.abs(corrected - ref))
    assert mae_after < 0.2 * mae_before


def test_fit_identity_and_grayscale_stability():
    clip = make_clip(n_frames=4)
    px = clip.reshape(-1, 3) * 255.0
    affine, stats = cc.fit_affine_color_transform(px, px)
    deviation = cc.affine_deviation(affine)
    assert deviation["max_abs_bias"] < 0.1
    assert deviation["matrix_frobenius"] < 0.01
    assert stats["mae_after"] < 0.01

    # Near-grayscale (rank-deficient) input: ridge keeps the solve stable and
    # near identity instead of exploding.
    gray = np.repeat(np.linspace(5, 200, 5000).reshape(-1, 1), 3, axis=1)
    affine_gray, _ = cc.fit_affine_color_transform(gray, gray)
    deviation_gray = cc.affine_deviation(affine_gray)
    assert deviation_gray["max_abs_bias"] < 1.0
    assert deviation_gray["matrix_frobenius"] < 0.1


def test_serialize_round_trip_and_json_safe():
    affine = known_affine()
    payload = cc.serialize_affine(affine)
    json.dumps(payload)
    rebuilt = cc.deserialize_affine(payload)
    assert rebuilt is not None
    assert np.allclose(rebuilt, affine, atol=1e-4)

    assert cc.deserialize_affine(None) is None
    assert cc.deserialize_affine({}) is None
    assert cc.deserialize_affine({"matrix": [[1, 0], [0, 1]], "bias": [0, 0, 0]}) is None


def test_transform_tensor_matches_plain_conversion_when_affine_none():
    clip = make_clip(n_frames=3, height=9, width=11)
    for mode in ("truncate", "round"):
        expected = media_helpers.tensor_to_uint8_frames(clip, mode=mode)
        actual = cc.transform_tensor_to_uint8_frames(clip, None, mode=mode)
        assert np.array_equal(expected, actual)

    with pytest.raises(ValueError):
        cc.transform_tensor_to_uint8_frames(clip, None, mode="banana")
    with pytest.raises(ValueError):
        cc.transform_tensor_to_uint8_frames(clip, known_affine(), mode="banana")


def test_transform_tensor_applies_affine_without_mutating_input():
    clip = make_clip(n_frames=3)
    snapshot = clip.copy()
    affine = known_affine()

    out = cc.transform_tensor_to_uint8_frames(clip, affine, mode="round")

    assert np.array_equal(clip, snapshot)
    assert out.dtype == np.uint8
    assert out.shape == clip.shape
    expected = apply_affine_to_clip01(clip, affine)
    expected_uint8 = np.rint(expected.astype(np.float64) * 255.0).clip(0, 255).astype(np.uint8)
    assert np.max(np.abs(out.astype(int) - expected_uint8.astype(int))) <= 1


def test_transform_tensor_torch_parity():
    torch = pytest.importorskip("torch")
    clip = make_clip(n_frames=3)
    affine = known_affine()
    expected = cc.transform_tensor_to_uint8_frames(clip, affine, mode="round")

    tensor = torch.from_numpy(clip.copy())
    assert np.array_equal(cc.transform_tensor_to_uint8_frames(tensor, affine, mode="round"), expected)

    grad_tensor = torch.from_numpy(clip.copy()).requires_grad_(True)
    assert np.array_equal(cc.transform_tensor_to_uint8_frames(grad_tensor, affine, mode="round"), expected)

    wide = torch.from_numpy(np.concatenate([clip.copy(), clip.copy()], axis=-1))
    non_contiguous = wide[..., :3]
    assert not non_contiguous.is_contiguous()
    assert np.array_equal(cc.transform_tensor_to_uint8_frames(non_contiguous, affine, mode="round"), expected)


# --- Reference index selection ---------------------------------------------


def test_select_indices_margin_from_step():
    indices = cc.select_context_reference_indices(
        frame_count=100, source_frame_count=100,
        mask_start_frame=25, mask_end_frame=75, step=8,
        max_frames=10_000,
    )
    assert indices == list(range(0, 17)) + list(range(83, 100))


def test_select_indices_default_margin_without_constraint():
    indices = cc.select_context_reference_indices(
        frame_count=40, source_frame_count=40,
        mask_start_frame=10, mask_end_frame=30, step=1,
    )
    assert indices == list(range(0, 9)) + list(range(31, 40))


def test_select_indices_excludes_padding_via_source_frame_count():
    # Real LTX case pinned by tests/test_editor_node.py: frame_count=169,
    # source=168, mask 49->169, step 8. Padding sits inside the mask span, the
    # tail is empty, and the head loses one latent block of margin.
    indices = cc.select_context_reference_indices(
        frame_count=169, source_frame_count=168,
        mask_start_frame=49, mask_end_frame=169, step=8,
        max_frames=10_000,
    )
    assert indices == list(range(0, 41))


def test_select_indices_mask_covers_everything():
    indices = cc.select_context_reference_indices(
        frame_count=97, source_frame_count=97,
        mask_start_frame=0, mask_end_frame=97, step=8,
    )
    assert indices == []


def test_select_indices_subsamples_to_max_frames():
    full = cc.select_context_reference_indices(
        frame_count=200, source_frame_count=200,
        mask_start_frame=90, mask_end_frame=120, step=1,
        max_frames=10_000,
    )
    capped = cc.select_context_reference_indices(
        frame_count=200, source_frame_count=200,
        mask_start_frame=90, mask_end_frame=120, step=1,
    )
    assert len(full) > cc.DRIFT_MAX_REFERENCE_FRAMES
    assert len(capped) <= cc.DRIFT_MAX_REFERENCE_FRAMES
    assert capped == sorted(capped)
    assert set(capped).issubset(set(full))
    assert capped[0] == full[0] and capped[-1] == full[-1]


# --- Stash -----------------------------------------------------------------


def test_stash_quantizes_with_rint_not_truncation():
    value = 100.7 / 255.0
    clip = np.full((8, 16, 16, 3), value, dtype=np.float32)
    _, stash = make_stash_context(clip, mask_start=4, mask_end=6)
    assert np.all(stash["frames"] == 101)


def test_stash_none_when_mask_covers_everything():
    clip = make_clip(n_frames=8)
    stash = cc.build_context_reference_stash(
        clip, frame_count=8, source_frame_count=8,
        mask_start_frame=0, mask_end_frame=8, frame_constraint=None,
    )
    assert stash is None


def test_stash_shape_and_metadata():
    clip = make_clip(n_frames=32)
    ctx, stash = make_stash_context(clip, mask_start=12, mask_end=24)
    assert stash["frame_count"] == 32
    assert stash["indices"] == list(range(0, 11)) + list(range(25, 32))
    assert stash["frames"].shape == (len(stash["indices"]), 36, 64, 3)
    assert stash["frames"].dtype == np.uint8
    assert cc.DRIFT_STASH_CONTEXT_KEY.startswith("_")  # stripped from public ctx by convention


# --- fit_drift_correction orchestration -------------------------------------


def test_drift_correction_end_to_end():
    clip = make_clip(n_frames=32)
    ctx, stash = make_stash_context(clip, mask_start=12, mask_end=24)
    drifted = (clip - 2.0 / 255.0).clip(0.0, 1.0).astype(np.float32)

    affine, record = cc.fit_drift_correction(drifted, ctx, enabled=True)

    assert affine is not None
    assert record["applied"] is True
    assert record["model"] == cc.DRIFT_MODEL_NAME
    # Uniform drift on both anchor sides -> the head/tail fits agree and
    # collapse to one pooled global transform.
    assert record["mode"] == "global"
    assert record["frames_used"] == stash["indices"]
    assert len(record["matrix"]) == 3 and all(len(row) == 3 for row in record["matrix"])
    assert len(record["bias"]) == 3
    assert record["residual_after"]["mae"] < record["residual_before"]["mae"]
    assert record["residual_after"]["mae"] < 0.5
    json.dumps(record)

    # Applying the correction moves the whole clip back toward the reference.
    corrected = cc.transform_tensor_to_uint8_frames(drifted, affine, mode="round")
    reference = media_helpers.tensor_to_uint8_frames(clip, mode="round")
    uncorrected = media_helpers.tensor_to_uint8_frames(drifted, mode="round")
    mae_corrected = np.mean(np.abs(corrected.astype(int) - reference.astype(int)))
    mae_uncorrected = np.mean(np.abs(uncorrected.astype(int) - reference.astype(int)))
    assert mae_corrected < 0.35 * mae_uncorrected


def test_drift_correction_clamp_blocks_large_grade():
    clip = make_clip(n_frames=32)
    ctx, _ = make_stash_context(clip, mask_start=12, mask_end=24)
    graded = (clip - 30.0 / 255.0).clip(0.0, 1.0).astype(np.float32)

    affine, record = cc.fit_drift_correction(graded, ctx, enabled=True)

    assert affine is None
    assert record["applied"] is False
    assert record["skip_reason"] == cc.DRIFT_SKIP_CLAMP_EXCEEDED
    assert record["clamp"]["measured_max_abs_bias"] > cc.DRIFT_MAX_BIAS
    json.dumps(record)


def test_drift_correction_skip_reasons():
    clip = make_clip(n_frames=32)
    ctx, _ = make_stash_context(clip, mask_start=12, mask_end=24)

    affine, record = cc.fit_drift_correction(clip, ctx, enabled=False)
    assert affine is None and record["skip_reason"] == cc.DRIFT_SKIP_TOGGLE_OFF

    affine, record = cc.fit_drift_correction(clip, {}, enabled=True)
    assert affine is None and record["skip_reason"] == cc.DRIFT_SKIP_NO_REFERENCE

    affine, record = cc.fit_drift_correction(clip, None, enabled=True)
    assert affine is None and record["skip_reason"] == cc.DRIFT_SKIP_NO_REFERENCE

    affine, record = cc.fit_drift_correction(clip[:30], ctx, enabled=True)
    assert affine is None and record["skip_reason"] == cc.DRIFT_SKIP_FRAME_COUNT_MISMATCH
    assert record["incoming_frames"] == 30 and record["expected_frames"] == 32

    rgba = np.concatenate([clip, np.ones_like(clip[..., :1])], axis=-1)
    affine, record = cc.fit_drift_correction(rgba, ctx, enabled=True)
    assert affine is None and record["skip_reason"] == cc.DRIFT_SKIP_UNSUPPORTED_CHANNELS

    tiny_ctx = {
        cc.DRIFT_STASH_CONTEXT_KEY: {
            "indices": [0],
            "frames": np.zeros((1, 8, 8, 3), dtype=np.uint8),
            "long_edge": cc.DRIFT_REFERENCE_LONG_EDGE,
            "frame_count": 32,
        }
    }
    affine, record = cc.fit_drift_correction(clip, tiny_ctx, enabled=True)
    assert affine is None and record["skip_reason"] == cc.DRIFT_SKIP_INSUFFICIENT_PIXELS

    for rec in (record,):
        json.dumps(rec)


def test_drift_correction_identity_when_no_drift():
    clip = make_clip(n_frames=32)
    ctx, _ = make_stash_context(clip, mask_start=12, mask_end=24)

    affine, record = cc.fit_drift_correction(clip, ctx, enabled=True)

    # Undrifted input fits a near-identity transform (stash uint8 quantization
    # is the only residual source); correction stays within clamps and is
    # effectively a no-op.
    assert affine is not None
    assert record["mode"] == "global"
    deviation = cc.affine_deviation(affine)
    assert deviation["max_abs_bias"] < 1.0
    assert deviation["matrix_frobenius"] < 0.02


# --- Dual-anchor blended correction ------------------------------------------


def apply_split_drift(clip01, mask_start, mask_end, bias_head_255, bias_tail_255):
    """Per-frame bias drift: bias_head before the mask window, bias_tail after,
    linear ramp across the generated span — the operator the blend must cancel."""
    out = clip01.copy()
    n = clip01.shape[0]
    for i in range(n):
        if i <= mask_start:
            b = bias_head_255
        elif i >= mask_end:
            b = bias_tail_255
        else:
            w = (i - mask_start) / float(mask_end - mask_start)
            b = (1.0 - w) * bias_head_255 + w * bias_tail_255
        out[i] = out[i] + b / 255.0
    return out.clip(0.0, 1.0).astype(np.float32)


def test_blended_correction_when_anchor_sides_disagree():
    clip = make_clip(n_frames=48)
    ctx, stash = make_stash_context(clip, mask_start=16, mask_end=32)
    assert stash["mask_start_frame"] == 16 and stash["mask_end_frame"] == 32
    drifted = apply_split_drift(clip, 16, 32, -4.0, 3.0)

    correction, record = cc.fit_drift_correction(drifted, ctx, enabled=True)

    assert correction is not None and isinstance(correction, dict)
    assert record["mode"] == "blended"
    assert record["ramp"] == {"start": 16, "end": 32}
    assert record["head"]["frames_used"] == list(range(0, 15))
    assert record["tail"]["frames_used"] == list(range(33, 48))
    # Correction direction is drifted -> reference, so signs invert. The ridge
    # may split a pure bias between the bias and matrix terms (they are
    # correlated on mid-range content), so assert the FUNCTIONAL shift at
    # mid-gray — invariant to that parameter split — not the raw bias value.
    gray = np.full((1, 1, 3), 128.0, dtype=np.float32)
    head_shift = float(np.mean(cc.apply_affine_color_transform(
        gray, cc.deserialize_affine(record["head"])) - gray))
    tail_shift = float(np.mean(cc.apply_affine_color_transform(
        gray, cc.deserialize_affine(record["tail"])) - gray))
    assert abs(head_shift - 4.0) < 0.5
    assert abs(tail_shift + 3.0) < 0.5
    json.dumps(record)

    # The blended apply restores the whole clip — including the ramped middle —
    # while a single global fit could only satisfy one seam.
    corrected = cc.transform_tensor_to_uint8_frames(drifted, correction, mode="round")
    reference = media_helpers.tensor_to_uint8_frames(clip, mode="round")
    for probe in (0, 8, 16, 20, 24, 28, 32, 40, 47):
        frame_mae = np.mean(np.abs(corrected[probe].astype(int) - reference[probe].astype(int)))
        assert frame_mae < 0.8, f"frame {probe} mae {frame_mae}"


def test_blend_collapses_to_global_when_sides_agree():
    clip = make_clip(n_frames=48)
    ctx, _ = make_stash_context(clip, mask_start=16, mask_end=32)
    drifted = (clip - 2.0 / 255.0).clip(0.0, 1.0).astype(np.float32)

    correction, record = cc.fit_drift_correction(drifted, ctx, enabled=True)

    assert isinstance(correction, np.ndarray)
    assert record["mode"] == "global"
    assert record.get("blend_collapsed") is True


def test_blend_falls_back_to_global_when_one_side_underpixeled():
    # Tail domain is a single frame (2304 px < DRIFT_MIN_FIT_PIXELS) -> pooled
    # global fit, no blended mode.
    clip = make_clip(n_frames=40)
    ctx, stash = make_stash_context(clip, mask_start=16, mask_end=38)
    assert len([i for i in stash["indices"] if i >= 38]) == 1
    drifted = apply_split_drift(clip, 16, 38, -4.0, 3.0)

    correction, record = cc.fit_drift_correction(drifted, ctx, enabled=True)

    assert isinstance(correction, np.ndarray)
    assert record["mode"] == "global"
    assert "blend_collapsed" not in record  # fallback, not an agreed dual fit


def test_blend_clamp_on_one_side_skips_entirely():
    clip = make_clip(n_frames=48)
    ctx, _ = make_stash_context(clip, mask_start=16, mask_end=32)
    drifted = apply_split_drift(clip, 16, 32, -30.0, -2.0)

    correction, record = cc.fit_drift_correction(drifted, ctx, enabled=True)

    assert correction is None
    assert record["skip_reason"] == cc.DRIFT_SKIP_CLAMP_EXCEEDED
    assert record["clamp_side"] == "head"


def test_schedule_application_ramps_between_transforms():
    identity = np.concatenate([np.eye(3), np.zeros((3, 1))], axis=1)
    head = identity.copy(); head[:, 3] = 10.0
    tail = identity.copy(); tail[:, 3] = -10.0
    schedule = {"head": head, "tail": tail, "ramp_start": 10, "ramp_end": 20}
    clip = np.full((30, 8, 8, 3), 0.5, dtype=np.float32)

    out = cc.transform_tensor_to_uint8_frames(clip, schedule, mode="round").astype(int)

    base = int(np.rint(0.5 * 255))
    assert np.all(out[0] == base + 10) and np.all(out[10] == base + 10)
    assert np.all(out[20] == base - 10) and np.all(out[29] == base - 10)
    assert np.all(out[15] == base)  # midpoint of the ramp
    means = [float(np.mean(out[i])) for i in range(10, 21)]
    assert all(means[k] >= means[k + 1] for k in range(len(means) - 1))  # monotonic ease
