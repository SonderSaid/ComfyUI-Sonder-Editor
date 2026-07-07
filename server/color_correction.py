"""Color transform fitting and application.

Generic foundation for color correction/grading: supervised affine color
transforms fitted from paired pixels, serialized in the same 3x4 float64
``cv2.transform`` convention as ``media_helpers.rgb_color_correction_affine``.

The drift-orchestration section below applies the generic core to VAE
color-drift cancellation in chained renders: the editor stashes downsampled
copies of the mask-protected context frames it emitted; the save node pairs
the returned frames at the same indices, fits a ridge-affine transform
(incoming -> reference), and applies it to the whole clip before encode.
Pairing is deterministic from the execution context — never detected by
frame similarity (an off-by-one pairing poisons the fit).
"""

import logging

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# --- Generic fit defaults ---
RIDGE_LAMBDA = 1e-3  # relative (mean-form), 0..1 domain; stabilizes narrow-gamut fits toward identity
SERIALIZE_MATRIX_DECIMALS = 6
SERIALIZE_BIAS_DECIMALS = 4

# --- Drift-correction orchestration ---
DRIFT_STASH_CONTEXT_KEY = "_color_drift_reference"
COLOR_DRIFT_METADATA_KEY = "color_drift_correction"
DRIFT_MODEL_NAME = "ridge_affine_3x4"
DRIFT_REFERENCE_LONG_EDGE = 320    # resolution the validation diagnostic ran at
DRIFT_MAX_REFERENCE_FRAMES = 32    # stash cap ~5.5MB at 16:9 uint8
DRIFT_MIN_REFERENCE_FRAMES = 1
DRIFT_MIN_FIT_PIXELS = 10_000
DRIFT_MAX_BIAS = 10.0              # 0..255 units; ~4x worst measured accumulation, below deliberate grades
DRIFT_MAX_MATRIX_DEVIATION = 0.15  # Frobenius ||M - I||; saturation/contrast grades exceed this
DRIFT_BOUNDARY_MARGIN_DEFAULT = 1  # frames, when no frame constraint declares the latent block size
DRIFT_BLEND_COLLAPSE_BIAS = 0.25     # 0..255; head/tail fits closer than this collapse to one global fit
DRIFT_BLEND_COLLAPSE_MATRIX = 0.005  # Frobenius diff; matrix half of the same collapse test

DRIFT_SKIP_TOGGLE_OFF = "toggle_off"
DRIFT_SKIP_NO_REFERENCE = "no_reference_frames"
DRIFT_SKIP_UNSUPPORTED_CHANNELS = "unsupported_channels"
DRIFT_SKIP_FRAME_COUNT_MISMATCH = "frame_count_mismatch"
DRIFT_SKIP_INSUFFICIENT_PIXELS = "insufficient_pixels"
DRIFT_SKIP_CLAMP_EXCEEDED = "clamp_exceeded"
DRIFT_SKIP_FIT_ERROR = "fit_error"


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_cpu_array(tensor):
    """Duck-typed torch/numpy batch access without mutating or copying the whole input."""
    if hasattr(tensor, "detach"):
        return tensor.detach().cpu()
    return np.asarray(tensor)


# ---------------------------------------------------------------------------
# Generic core — reusable for future color correction/grading features.
# ---------------------------------------------------------------------------


def fit_affine_color_transform(source_rgb, target_rgb, *, ridge_lambda: float = RIDGE_LAMBDA):
    """Fit an affine color transform mapping source pixels onto target pixels.

    Inputs are float arrays of identical shape ``(..., 3)`` in the 0..255
    domain. The fit solves closed-form ridge normal equations regularized
    TOWARD THE IDENTITY affine (matrix toward I, bias toward 0), so
    ill-conditioned inputs (flat/dark content) degrade gracefully to a
    near-identity result instead of exploding.

    Returns ``(affine, stats)`` where ``affine`` is the 3x4 float64
    ``cv2.transform`` convention (linear part unitless, bias in 0..255) and
    ``stats`` carries MAE before/after on the fit pairs in 0..255 units.
    """
    src = np.asarray(source_rgb, dtype=np.float64).reshape(-1, 3) / 255.0
    tgt = np.asarray(target_rgb, dtype=np.float64).reshape(-1, 3) / 255.0
    if src.shape != tgt.shape or src.shape[0] == 0:
        raise ValueError("source/target pixel arrays must be non-empty and identically shaped")

    n = src.shape[0]
    x = np.concatenate([src, np.ones((n, 1), dtype=np.float64)], axis=1)  # [n,4]
    w_identity = np.zeros((4, 3), dtype=np.float64)
    w_identity[:3, :3] = np.eye(3)

    xtx = x.T @ x / n
    xty = x.T @ tgt / n
    reg = ridge_lambda * np.eye(4)
    weights = np.linalg.solve(xtx + reg, xty + reg @ w_identity)  # [4,3]

    fitted = x @ weights
    mae_before_pc = np.mean(np.abs(src - tgt), axis=0) * 255.0
    mae_after_pc = np.mean(np.abs(fitted - tgt), axis=0) * 255.0

    affine = np.zeros((3, 4), dtype=np.float64)
    affine[:, :3] = weights[:3, :].T
    affine[:, 3] = weights[3, :] * 255.0

    stats = {
        "n_pixels": int(n),
        "mae_before": float(np.mean(mae_before_pc)),
        "mae_after": float(np.mean(mae_after_pc)),
        "mae_before_per_channel": [float(v) for v in mae_before_pc],
        "mae_after_per_channel": [float(v) for v in mae_after_pc],
    }
    return affine, stats


def apply_affine_color_transform(frame_rgb: np.ndarray, affine) -> np.ndarray:
    """Apply a 3x4 affine to an RGB frame; ``None`` is a passthrough.

    uint8 in -> saturated uint8 out; float in -> float out (cv2.transform).
    """
    if affine is None:
        return frame_rgb
    return cv2.transform(frame_rgb, np.asarray(affine, dtype=np.float64))


def affine_deviation(affine) -> dict:
    arr = np.asarray(affine, dtype=np.float64)
    return {
        "max_abs_bias": float(np.max(np.abs(arr[:, 3]))),
        "matrix_frobenius": float(np.linalg.norm(arr[:, :3] - np.eye(3))),
    }


def affine_within_limits(affine, *, max_bias: float, max_matrix_deviation: float):
    deviation = affine_deviation(affine)
    ok = (
        deviation["max_abs_bias"] <= max_bias
        and deviation["matrix_frobenius"] <= max_matrix_deviation
    )
    return ok, deviation


def serialize_affine(
    affine,
    *,
    matrix_decimals: int = SERIALIZE_MATRIX_DECIMALS,
    bias_decimals: int = SERIALIZE_BIAS_DECIMALS,
) -> dict:
    arr = np.asarray(affine, dtype=np.float64)
    return {
        "matrix": [[round(float(v), matrix_decimals) for v in row] for row in arr[:, :3]],
        "bias": [round(float(v), bias_decimals) for v in arr[:, 3]],
    }


def deserialize_affine(payload) -> np.ndarray | None:
    """Rebuild a 3x4 affine from `serialize_affine` output. Tolerant of bad input."""
    try:
        matrix = np.asarray(payload["matrix"], dtype=np.float64)
        bias = np.asarray(payload["bias"], dtype=np.float64)
        if matrix.shape != (3, 3) or bias.shape != (3,):
            return None
        return np.concatenate([matrix, bias.reshape(3, 1)], axis=1)
    except (TypeError, KeyError, ValueError, IndexError):
        return None


def downsample_rgb_for_analysis(frame_rgb: np.ndarray, *, long_edge: int | None = None,
                                size: tuple[int, int] | None = None) -> np.ndarray:
    """Downsample a frame for color statistics with INTER_AREA (anti-aliased).

    Statistics resampling must not sharpen (LANCZOS ringing skews fits), so
    this deliberately does not reuse `resize_frame_to_long_edge`. Pass
    ``size=(w, h)`` to match another analysis frame's exact stored shape;
    stash and incoming sides MUST share this one code path.
    """
    h, w = frame_rgb.shape[:2]
    if size is not None:
        dst_w, dst_h = int(size[0]), int(size[1])
    else:
        edge = int(long_edge or 0)
        scale = min(1.0, edge / max(w, h)) if edge > 0 else 1.0
        dst_w = max(1, int(round(w * scale)))
        dst_h = max(1, int(round(h * scale)))
    if dst_w == w and dst_h == h:
        return frame_rgb
    return cv2.resize(frame_rgb, (dst_w, dst_h), interpolation=cv2.INTER_AREA)


def _affine_for_frame(correction, frame_index: int) -> np.ndarray:
    """Resolve a single affine (3x4) or blended schedule to this frame's affine.

    A schedule is `{"head", "tail", "ramp_start", "ramp_end"}`: pure head
    transform up to `ramp_start` (last frozen pre-context latent block), pure
    tail from `ramp_end` on, linear interpolation across the generated span in
    between. Affine parameters live in a linear space, so the lerp is smooth
    and each per-frame step is tiny — no flicker by construction.
    """
    if isinstance(correction, np.ndarray):
        return correction
    head = correction["head"]
    tail = correction["tail"]
    start = correction["ramp_start"]
    end = correction["ramp_end"]
    if frame_index <= start:
        return head
    if frame_index >= end:
        return tail
    weight = (frame_index - start) / float(end - start)
    return (1.0 - weight) * head + weight * tail


def transform_tensor_to_uint8_frames(tensor, correction, *, mode: str = "truncate") -> np.ndarray:
    """`tensor_to_uint8_frames` with a color correction fused in.

    `correction` is None (plain delegate), a single 3x4 affine, or a blended
    head/tail schedule (see `_affine_for_frame`). Semantics match
    media_helpers.tensor_to_uint8_frames exactly (scale x255, transform, then
    rint for "round" / nothing for "truncate", clip, uint8), processed per
    frame into a preallocated output. NEVER mutates the input tensor —
    ComfyUI caches node outputs, so an in-place correction would double-apply
    on re-runs and leak into parallel consumers.
    """
    from .media_helpers import tensor_to_uint8_frames

    if mode not in ("round", "truncate"):
        raise ValueError(f"Unknown tensor conversion mode: {mode}")
    if correction is None:
        return tensor_to_uint8_frames(tensor, mode=mode)

    batch = _as_cpu_array(tensor)
    if len(batch.shape) != 4 or batch.shape[-1] != 3:
        raise ValueError(f"expected (N,H,W,3) image batch, got shape {tuple(batch.shape)}")
    if isinstance(correction, np.ndarray):
        correction = np.asarray(correction, dtype=np.float64)

    out = np.empty(tuple(batch.shape), dtype=np.uint8)
    for i in range(batch.shape[0]):
        affine = np.asarray(_affine_for_frame(correction, i), dtype=np.float64)
        frame = np.ascontiguousarray(np.asarray(batch[i], dtype=np.float32) * 255.0)
        frame = cv2.transform(frame, affine)
        if mode == "round":
            frame = np.rint(frame)
        out[i] = frame.clip(0, 255).astype(np.uint8)
    return out


# ---------------------------------------------------------------------------
# VAE drift-correction orchestration (chained renders).
# ---------------------------------------------------------------------------


def select_context_reference_indices(*, frame_count: int, source_frame_count: int,
                                     mask_start_frame: int, mask_end_frame: int,
                                     step: int,
                                     max_frames: int = DRIFT_MAX_REFERENCE_FRAMES) -> list:
    """Frame indices safe to use as drift references.

    Mask-protected head/tail regions, shrunk by a latent-block margin at each
    mask boundary (block blending contaminates the seam), and bounded above by
    ``source_frame_count`` so padded frames (last-frame repeats; the padded
    span is inside ``mask_end_frame`` by construction) are never selected.
    """
    frame_count = max(0, _coerce_int(frame_count, 0))
    source_frame_count = max(0, _coerce_int(source_frame_count, 0))
    mask_start_frame = max(0, _coerce_int(mask_start_frame, 0))
    mask_end_frame = max(0, _coerce_int(mask_end_frame, 0))
    step = _coerce_int(step, 1)
    margin = step if step > 1 else DRIFT_BOUNDARY_MARGIN_DEFAULT

    real_end = min(source_frame_count, frame_count)
    head_end = min(mask_start_frame - margin, real_end)
    tail_start = mask_end_frame + margin
    indices = list(range(0, max(0, head_end)))
    if tail_start < real_end:
        indices.extend(range(tail_start, real_end))

    if len(indices) > max_frames > 0:
        picks = np.linspace(0, len(indices) - 1, max_frames)
        indices = sorted({indices[int(round(p))] for p in picks})
    return indices


def build_context_reference_stash(frames_tensor, *, frame_count: int, source_frame_count: int,
                                  mask_start_frame: int, mask_end_frame: int,
                                  frame_constraint=None):
    """Editor-side stash of downsampled protected context frames.

    Frames arrive as the editor's rendered tensor (N,H,W,3 float 0..1); stored
    downsampled uint8 via np.rint — truncation would inject a systematic
    -0.5/255 reference bias (the corrector would then CREATE drift). Returns
    None (never raises) when no usable reference frames exist; a stash
    failure must never fail a render.
    """
    try:
        step = 1
        if isinstance(frame_constraint, dict):
            step = _coerce_int(frame_constraint.get("step"), 1)
        indices = select_context_reference_indices(
            frame_count=frame_count,
            source_frame_count=source_frame_count,
            mask_start_frame=mask_start_frame,
            mask_end_frame=mask_end_frame,
            step=step,
        )
        if len(indices) < DRIFT_MIN_REFERENCE_FRAMES:
            return None

        batch = _as_cpu_array(frames_tensor)
        if len(batch.shape) != 4 or batch.shape[-1] != 3 or batch.shape[0] <= max(indices):
            return None

        stash_frames = []
        target_size = None
        for idx in indices:
            frame = np.asarray(batch[idx], dtype=np.float32)
            if target_size is None:
                ds = downsample_rgb_for_analysis(frame, long_edge=DRIFT_REFERENCE_LONG_EDGE)
                target_size = (ds.shape[1], ds.shape[0])
            else:
                ds = downsample_rgb_for_analysis(frame, size=target_size)
            stash_frames.append(np.rint(ds * 255.0).clip(0, 255).astype(np.uint8))

        return {
            "indices": [int(i) for i in indices],
            "frames": np.stack(stash_frames, axis=0),
            "long_edge": DRIFT_REFERENCE_LONG_EDGE,
            "frame_count": int(frame_count),
            # Mask boundaries let the save-side fit split head/tail anchor
            # groups and place the blend ramp across the generated span.
            "mask_start_frame": max(0, _coerce_int(mask_start_frame, 0)),
            "mask_end_frame": max(0, _coerce_int(mask_end_frame, 0)),
        }
    except Exception as exc:  # noqa: BLE001 - stash is best-effort by contract
        logger.debug("color drift reference stash failed: %s", exc)
        return None


def _skip_record(enabled: bool, reason: str, **extra) -> dict:
    record = {"enabled": bool(enabled), "applied": False, "model": DRIFT_MODEL_NAME,
              "skip_reason": reason}
    record.update(extra)
    return record


def _residuals_from_stats(stats: dict) -> tuple[dict, dict]:
    before = {
        "mae": round(stats["mae_before"], 4),
        "per_channel": [round(v, 4) for v in stats["mae_before_per_channel"]],
    }
    after = {
        "mae": round(stats["mae_after"], 4),
        "per_channel": [round(v, 4) for v in stats["mae_after_per_channel"]],
    }
    return before, after


def _fit_clamped(incoming_px: np.ndarray, reference_px: np.ndarray):
    """Fit + clamp check. Returns (affine, stats, None) or (None, None, clamp_details)."""
    affine, stats = fit_affine_color_transform(incoming_px, reference_px)
    ok, deviation = affine_within_limits(
        affine, max_bias=DRIFT_MAX_BIAS, max_matrix_deviation=DRIFT_MAX_MATRIX_DEVIATION,
    )
    if ok:
        return affine, stats, None
    return None, None, {
        "max_bias": DRIFT_MAX_BIAS,
        "max_matrix_deviation": DRIFT_MAX_MATRIX_DEVIATION,
        "measured_max_abs_bias": round(deviation["max_abs_bias"], 4),
        "measured_matrix_frobenius": round(deviation["matrix_frobenius"], 6),
    }


def _side_record(affine: np.ndarray, stats: dict, indices: list) -> dict:
    serialized = serialize_affine(affine)
    before, after = _residuals_from_stats(stats)
    return {
        "matrix": serialized["matrix"],
        "bias": serialized["bias"],
        "frames_used": [int(i) for i in indices],
        "n_pixels": stats["n_pixels"],
        "residual_before": before,
        "residual_after": after,
    }


def fit_drift_correction(frames_tensor, execution_context, *, enabled: bool = True):
    """Fit the drift-cancelling correction for a save node's incoming frames.

    Returns ``(correction or None, metadata_record)``; never raises. The
    correction is a single 3x4 affine (record ``mode: "global"``) or, when the
    pre- and post-context anchors carry genuinely different color states (a
    regeneration bridging takes from different generations), a blended
    head/tail schedule (``mode: "blended"``) that eases linearly across the
    generated span so the clip matches BOTH neighbors at its seams. The record
    is JSON-safe and attaches to asset ``generation_params`` under
    ``COLOR_DRIFT_METADATA_KEY`` on both applied and skipped outcomes.
    """
    try:
        if not enabled:
            return None, _skip_record(enabled, DRIFT_SKIP_TOGGLE_OFF)

        context = execution_context if isinstance(execution_context, dict) else {}
        stash = context.get(DRIFT_STASH_CONTEXT_KEY)
        reference = stash.get("frames") if isinstance(stash, dict) else None
        indices = stash.get("indices") if isinstance(stash, dict) else None
        if (
            not isinstance(reference, np.ndarray)
            or reference.size == 0
            or not indices
            or len(indices) != reference.shape[0]
        ):
            return None, _skip_record(enabled, DRIFT_SKIP_NO_REFERENCE)

        batch = _as_cpu_array(frames_tensor)
        if len(batch.shape) != 4 or batch.shape[-1] != 3:
            return None, _skip_record(
                enabled, DRIFT_SKIP_UNSUPPORTED_CHANNELS,
                incoming_shape=[int(v) for v in batch.shape],
            )

        stash_frame_count = _coerce_int(stash.get("frame_count"), -1)
        incoming_count = int(batch.shape[0])
        if incoming_count != stash_frame_count:
            return None, _skip_record(
                enabled, DRIFT_SKIP_FRAME_COUNT_MISMATCH,
                incoming_frames=incoming_count, expected_frames=stash_frame_count,
            )

        size = (int(reference.shape[2]), int(reference.shape[1]))
        mask_start = stash.get("mask_start_frame")
        mask_end = stash.get("mask_end_frame")
        can_split = mask_start is not None and mask_end is not None
        head_inc, head_ref, head_idx = [], [], []
        tail_inc, tail_ref, tail_idx = [], [], []
        for pos, idx in enumerate(indices):
            frame = np.asarray(batch[int(idx)], dtype=np.float32)
            inc = downsample_rgb_for_analysis(frame, size=size).reshape(-1, 3).astype(np.float64) * 255.0
            ref = reference[pos].astype(np.float64).reshape(-1, 3)
            if can_split and int(idx) >= _coerce_int(mask_end, 1 << 30):
                tail_inc.append(inc); tail_ref.append(ref); tail_idx.append(int(idx))
            else:
                head_inc.append(inc); head_ref.append(ref); head_idx.append(int(idx))

        head_px = int(sum(a.shape[0] for a in head_inc))
        tail_px = int(sum(a.shape[0] for a in tail_inc))
        total_px = head_px + tail_px
        if total_px < DRIFT_MIN_FIT_PIXELS:
            return None, _skip_record(
                enabled, DRIFT_SKIP_INSUFFICIENT_PIXELS, n_pixels=total_px,
            )

        # --- Dual-anchor path: both sides independently fittable. ---
        blend_collapsed = False
        if head_px >= DRIFT_MIN_FIT_PIXELS and tail_px >= DRIFT_MIN_FIT_PIXELS:
            affine_head, stats_head, clamp_head = _fit_clamped(
                np.concatenate(head_inc), np.concatenate(head_ref))
            affine_tail, stats_tail, clamp_tail = _fit_clamped(
                np.concatenate(tail_inc), np.concatenate(tail_ref))
            if clamp_head or clamp_tail:
                return None, _skip_record(
                    enabled, DRIFT_SKIP_CLAMP_EXCEEDED,
                    clamp=clamp_head or clamp_tail,
                    clamp_side="head" if clamp_head else "tail",
                )
            diff = affine_head - affine_tail
            fits_agree = (
                float(np.max(np.abs(diff[:, 3]))) <= DRIFT_BLEND_COLLAPSE_BIAS
                and float(np.linalg.norm(diff[:, :3])) <= DRIFT_BLEND_COLLAPSE_MATRIX
            )
            if not fits_agree:
                ramp_start = max(0, _coerce_int(mask_start, 0))
                ramp_end = max(ramp_start + 1, min(_coerce_int(mask_end, incoming_count), incoming_count))
                correction = {
                    "head": affine_head,
                    "tail": affine_tail,
                    "ramp_start": ramp_start,
                    "ramp_end": ramp_end,
                }
                weight = head_px / float(total_px)
                combined_before = weight * stats_head["mae_before"] + (1 - weight) * stats_tail["mae_before"]
                combined_after = weight * stats_head["mae_after"] + (1 - weight) * stats_tail["mae_after"]
                record = {
                    "enabled": True,
                    "applied": True,
                    "model": DRIFT_MODEL_NAME,
                    "mode": "blended",
                    "frames_used": [int(i) for i in indices],
                    "n_pixels": total_px,
                    "ramp": {"start": ramp_start, "end": ramp_end},
                    "head": _side_record(affine_head, stats_head, head_idx),
                    "tail": _side_record(affine_tail, stats_tail, tail_idx),
                    "residual_before": {"mae": round(combined_before, 4)},
                    "residual_after": {"mae": round(combined_after, 4)},
                }
                return correction, record
            # Fits agree -> fall through to the pooled global fit below.
            blend_collapsed = True

        # --- Global path: one transform pooled over every anchor pair. ---
        incoming_px = np.concatenate(head_inc + tail_inc)
        reference_px = np.concatenate(head_ref + tail_ref)
        affine, stats, clamp = _fit_clamped(incoming_px, reference_px)
        if clamp:
            return None, _skip_record(enabled, DRIFT_SKIP_CLAMP_EXCEEDED, clamp=clamp)

        serialized = serialize_affine(affine)
        before, after = _residuals_from_stats(stats)
        record = {
            "enabled": True,
            "applied": True,
            "model": DRIFT_MODEL_NAME,
            "mode": "global",
            "matrix": serialized["matrix"],
            "bias": serialized["bias"],
            "frames_used": [int(i) for i in indices],
            "n_pixels": stats["n_pixels"],
            "residual_before": before,
            "residual_after": after,
        }
        if blend_collapsed:
            # Both anchor sides were fitted and agreed within the collapse
            # epsilon — distinguishes this from single-side global fits.
            record["blend_collapsed"] = True
        return affine, record
    except Exception as exc:  # noqa: BLE001 - correction must never fail a save
        logger.warning("color drift correction fit failed: %s", exc)
        return None, _skip_record(enabled, DRIFT_SKIP_FIT_ERROR, error=str(exc)[:200])
