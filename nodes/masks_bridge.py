"""Sonder Masks Bridge — per-channel video/audio generation-mask gating.

The Sonder Editor emits a single generation-mask window (`mask_start_time` /
`mask_end_time`, in seconds) describing which part of the render range is
generated; the pre/post context frames outside it are kept/conditioning.

This bridge exposes that one window as four mask-time outputs — separate
video and audio pairs — each gated by an Edit/Freeze toggle. Freezing a
channel collapses its window to zero width at `mask_start_time`, so nothing
is generated for it (the channel is kept from source and can drive the
other). This removes the friction of manually plugging/unplugging the
downstream temporal-mask nodes.

It additionally compiles that same window into hard 0/1 latent noise masks
when the matching latent and VAE are wired, so the window reaches the sampler
without a seconds round-trip and without a retyped fps. Two shapes are NOT
interchangeable, and both are load-bearing (see `comfy/utils.py reshape_mask`):

* video — a batch of `T_video` masks, one per **latent** frame. A batch at
  pixel-frame count resamples off by one latent at the head.
* audio — a **single** mask image shaped to the audio latent's last two axes.
  A batch never reaches a 4D latent's time axis at all; the axis itself is
  model-dependent (LTX `[B,C,T,F]`, MiniMax H3 `[B,C,F,T]`).

The pixel→latent map is `vae.downscale_ratio[0]`, a per-model callable, never
`downscale_index_formula` (a tiling stride, wrong for H3's fractional 17:5).
The audio rate is `vae.first_stage_model.latents_per_second`.

It reads the resolved mask frames from `project._execution_context`, which the
editor already computes identically for live and queued renders, so no queue-
snapshot resolution is needed for the region. FPS is resolved the way the
editor does (frozen job `scene_fps` for snapshot jobs, else live `scene.fps`,
else `project.fps`) so the seconds reproduce the editor's outputs exactly.
"""

from __future__ import annotations

import logging

try:
    import torch
except ImportError:  # pragma: no cover - ComfyUI always ships torch
    # Only the tensor emitters need it; the whole resolution core below stays
    # importable (and unit-testable) without it. A missing torch has exactly
    # one meaning, so the `exc.name` re-raise discipline used for `comfy_api`
    # version guards does not apply here.
    torch = None

logger = logging.getLogger(__name__)

# Audio axis detection tolerance, in latent steps. A real encode ceils through
# the mel pipeline while the computed length rounds, so the two legitimately
# disagree by one (measured: 252 encoded vs 251 computed on a 241-frame LTX
# window). Two is enough slack for that without admitting a frequency/stereo
# axis, which differs from the time axis by orders of magnitude.
AUDIO_AXIS_TOLERANCE = 2


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _resolve_active_scene(project):
    ctx = getattr(project, "_execution_context", None) or {}
    scene_id = ctx.get("scene_id", "")
    if scene_id:
        scene = project.get_scene(scene_id)
        if scene is not None:
            return scene
    scenes = getattr(project, "scenes", None) or []
    return scenes[0] if scenes else None


def _find_ref_job(project):
    """The pending/running job this execution rendered (peek OR consume)."""
    ctx = getattr(project, "_execution_context", None) or {}
    job_id = str(ctx.get("queue_job_ref_id", "") or "")
    if not job_id:
        return None
    for job in getattr(project, "generation_queue", []) or []:
        if getattr(job, "job_id", "") == job_id:
            from ..server.project_storage import hydrate_job
            return hydrate_job(project, job)
    return None


def _snapshot_version(queue_job) -> int:
    params = getattr(queue_job, "params", {}) or {}
    if not isinstance(params, dict):
        return 0
    return max(0, _coerce_int(params.get("snapshot_version", 0), 0))


def _resolve_fps(project) -> float:
    """Mirror SonderEditor's fps resolution so seconds match slots 15-16.

    Frozen job `scene_fps` (snapshot jobs) wins, else the live scene fps
    override, else the project fps.
    """
    job = _find_ref_job(project)
    if job is not None and _snapshot_version(job) > 0:
        fps = _coerce_float(getattr(job, "scene_fps", 0.0), 0.0)
        if fps > 0:
            return fps
        return _coerce_float(getattr(project, "fps", 0.0), 0.0)
    scene = _resolve_active_scene(project)
    if scene is not None:
        fps = _coerce_float(getattr(scene, "fps", 0.0), 0.0)
        if fps > 0:
            return fps
    return _coerce_float(getattr(project, "fps", 0.0), 0.0)


def resolve_mask_times(project, edit_video: bool = True, edit_audio: bool = True) -> dict:
    """Gate the editor's generation-mask window per channel (the testable core).

    Raises RuntimeError when the wired project carries no render context, so a
    misconfigured graph fails loud rather than emitting a silent zero window.

    Also returns the same gated window in window-local *pixel frames* plus the
    padded `frame_count`, which is what the latent-mask compilers index against.
    `mask_start_frame`/`mask_end_frame` are already tensor-local and snapped to
    the model's frame grid by the editor, so no rebasing happens here.
    """
    ctx = getattr(project, "_execution_context", None)
    if not isinstance(ctx, dict) or "mask_start_frame" not in ctx or "mask_end_frame" not in ctx:
        raise RuntimeError(
            "SonderMasksBridge: no Sonder render context found on the project. "
            "Wire this node downstream of a Sonder Editor that executed in this prompt."
        )

    fps = _resolve_fps(project)
    start_frame = _coerce_int(ctx.get("mask_start_frame"), 0)
    end_frame = _coerce_int(ctx.get("mask_end_frame"), 0)

    def to_sec(frame: int) -> float:
        return (frame / fps) if fps > 0 else 0.0

    full_start = to_sec(start_frame)
    full_end = to_sec(end_frame)

    video = (full_start, full_end) if edit_video else (full_start, full_start)
    audio = (full_start, full_end) if edit_audio else (full_start, full_start)
    # Freeze is the same rule in frame space: collapse to a zero-width window.
    video_frames = (start_frame, end_frame) if edit_video else (start_frame, start_frame)
    audio_frames = (start_frame, end_frame) if edit_audio else (start_frame, start_frame)

    return {
        "video_mask_start_time": video[0],
        "video_mask_end_time": video[1],
        "audio_mask_start_time": audio[0],
        "audio_mask_end_time": audio[1],
        "edit_video": bool(edit_video),
        "edit_audio": bool(edit_audio),
        "video_mask_frames": video_frames,
        "audio_mask_frames": audio_frames,
        "frame_count": _coerce_int(ctx.get("frame_count"), 0),
        # Read tolerantly: an older execution context predates the key and must yield 0
        # rather than raising. This fabricated tail is often kept rather than regenerated,
        # so recording it here is what makes a take self-describing.
        "frame_count_padding": max(0, _coerce_int(ctx.get("frame_count_padding"), 0)),
        "fps": fps,
    }


# ── Latent geometry, read from the wired VAEs ────────────────────────────────


def _pins_padding(result: dict, mask_frames) -> bool:
    """True when this channel keeps the fabricated grid padding instead of regenerating it.

    The pad occupies the tail of the tensor, so it is regenerated only by a mask that
    reaches `frame_count`. A Freeze (zero-width window) never does; neither does an Edit
    whose window ends before the tail, which is every render carrying post-context.
    """
    if _coerce_int(result.get("frame_count_padding"), 0) <= 0:
        return False
    return _coerce_int(mask_frames[1], 0) < _coerce_int(result.get("frame_count"), 0)


def video_downscale_fn(vae):
    """`vae.downscale_ratio[0]` — the per-model pixel-frames → latent-frames map.

    Video VAEs publish a `(callable, spatial, spatial)` tuple; image VAEs
    publish a bare int. Only the tuple form carries a temporal map, so anything
    else is refused rather than guessed at.
    """
    ratio = getattr(vae, "downscale_ratio", None)
    if isinstance(ratio, (tuple, list)) and len(ratio) == 3 and callable(ratio[0]):
        return ratio[0]
    raise RuntimeError(
        "SonderMasksBridge: the wired video VAE does not publish a temporal "
        "downscale map (`downscale_ratio` is {!r}). Wire the VAE that encoded "
        "this video latent — an image VAE cannot describe latent frames.".format(ratio)
    )


def audio_latents_per_second(vae) -> float:
    """`vae.first_stage_model.latents_per_second` — LTX 25.0, MiniMax H3 40.

    Lives on the inner model rather than the VAE wrapper and is not a public
    ComfyUI contract, so its absence is reported rather than defaulted. The
    wrapper's own `downscale_ratio` is NOT a substitute: it yields 40 for H3
    but 3.9 for LTX, whose true rate is 25.
    """
    inner = getattr(vae, "first_stage_model", None)
    rate = _coerce_float(getattr(inner, "latents_per_second", 0.0), 0.0)
    if rate > 0:
        return rate
    raise RuntimeError(
        "SonderMasksBridge: the wired audio VAE does not publish "
        "`first_stage_model.latents_per_second`, so its latent rate is unknown. "
        "Wire the audio VAE that encoded this audio latent."
    )


def _latent_shape(latent, socket: str, expected_rank: int):
    """Plain int tuple for a LATENT dict, or None when the socket is unwired."""
    if latent is None:
        return None
    if not isinstance(latent, dict) or "samples" not in latent:
        raise RuntimeError(
            "SonderMasksBridge: `{}` is not a LATENT (no `samples`).".format(socket)
        )
    samples = latent["samples"]
    # A joint AV latent reports `tensors[0].shape` and the MAX ndim across its
    # streams, so it passes a plain rank check and would silently produce a mask
    # for the wrong stream. Refuse it by identity instead.
    if getattr(samples, "is_nested", False):
        raise RuntimeError(
            "SonderMasksBridge: `{}` carries a joint AV latent (NestedTensor). "
            "Insert Separate AV Latent and wire the matching half.".format(socket)
        )
    shape = tuple(int(d) for d in getattr(samples, "shape", ()) or ())
    if len(shape) != expected_rank:
        raise RuntimeError(
            "SonderMasksBridge: `{}` must be a rank-{} latent, got shape {}. "
            "Core resamples masks by rank, so a mask built for this tensor "
            "would land on the wrong axes.".format(socket, expected_rank, list(shape))
        )
    return shape


def video_latent_index(downscale_fn, pixel: int) -> int:
    """Latent index for a window-local pixel frame.

    Boundaries reach here already snapped to the model's frame grid by the
    editor's `_snap_pixel_to_constraint`, so `downscale_fn` is only ever
    evaluated on grid points, where it is exact. Pixel 0 is special-cased:
    H3's map returns 1 (not 0) for a frame count of 0 or 1.
    """
    if pixel <= 0:
        return 0
    return max(0, _coerce_int(downscale_fn(int(pixel)), 0))


def resolve_video_mask_span(frame_count: int, latent_frames: int, start_pixel: int,
                            end_pixel: int, downscale_fn) -> tuple:
    """(lo, hi) latent indices for a window-local pixel span.

    Validates the wired latent against the render window BEFORE the zero-width
    early return, so a frozen channel cannot hide a geometry contradiction that
    would surface the moment the user unfreezes.
    """
    if frame_count <= 0:
        raise RuntimeError(
            "SonderMasksBridge: the render context carries no `frame_count`, so "
            "a video latent mask cannot be sized."
        )
    if latent_frames <= 0:
        raise RuntimeError("SonderMasksBridge: the video latent has no frames.")

    expected = max(0, _coerce_int(downscale_fn(int(frame_count)), 0))
    if expected != latent_frames:
        raise RuntimeError(
            "SonderMasksBridge: the wired video latent has {} latent frames, but "
            "this render window ({} frames) encodes to {}. The latent was not "
            "encoded from this window — check for a stale, upscaled, or "
            "wrong-source latent.".format(latent_frames, frame_count, expected)
        )

    if end_pixel <= start_pixel:
        return (0, 0)
    lo = min(video_latent_index(downscale_fn, start_pixel), latent_frames)
    hi = min(video_latent_index(downscale_fn, end_pixel), latent_frames)
    return (lo, max(lo, hi))


def detect_audio_time_axis(shape, expected_len: int) -> int:
    """-2 or -1, whichever of the audio latent's last two axes is time.

    Matched against the length the render window implies, which is exact rather
    than a plausible-rate band: at short durations LTX's fixed 16-bin frequency
    axis also looks like a plausible rate, and is longer than the time axis.
    """
    if expected_len <= 0:
        raise RuntimeError(
            "SonderMasksBridge: cannot derive the audio latent's expected length "
            "(fps or frame_count is zero), so its time axis cannot be identified."
        )
    deltas = {axis: abs(int(shape[axis]) - expected_len) for axis in (-2, -1)}
    hits = sorted((d, axis) for axis, d in deltas.items()
                  if d <= AUDIO_AXIS_TOLERANCE)
    detail = "axes ({}, {}) against an expected length of {}".format(
        shape[-2], shape[-1], expected_len)
    if not hits:
        raise RuntimeError(
            "SonderMasksBridge: neither of the audio latent's last two axes "
            "matches this render window — {}. The latent was not encoded from "
            "this window, or the wired audio VAE is not its encoder.".format(detail)
        )
    # On a short window the fixed frequency/stereo axis can also fall inside the
    # tolerance (e.g. LTX (17, 16) against 18). The nearer axis is still the
    # right one, so only a genuine tie is unresolvable.
    if len(hits) == 1 or hits[0][0] < hits[1][0]:
        return hits[0][1]
    raise RuntimeError(
        "SonderMasksBridge: the audio latent's time axis is ambiguous — "
        "{} are equally close.".format(detail)
    )


def resolve_audio_mask_span(frame_count: int, axis_len: int, start_pixel: int,
                            end_pixel: int) -> tuple:
    """(lo, hi) audio latent indices for a window-local pixel span.

    Uniform map — an audio VAE has no standalone first sample. FPS cancels
    algebraically (pixel/fps ÷ (frame_count/fps)), so this is exact integer
    arithmetic. Start floors and end ceils, matching the editor's own outward
    snapping of the pixel boundaries.
    """
    if frame_count <= 0:
        raise RuntimeError(
            "SonderMasksBridge: the render context carries no `frame_count`, so "
            "an audio latent mask cannot be sized."
        )
    if axis_len <= 0:
        raise RuntimeError("SonderMasksBridge: the audio latent has no time steps.")
    if end_pixel <= start_pixel:
        return (0, 0)
    lo = (int(start_pixel) * axis_len) // frame_count
    hi = -((-int(end_pixel) * axis_len) // frame_count)
    lo = max(0, min(lo, axis_len))
    return (lo, max(lo, min(hi, axis_len)))


# ── Tensor emitters (the only torch in this module) ──────────────────────────


def _require_torch():
    if torch is None:  # pragma: no cover - ComfyUI always ships torch
        raise RuntimeError(
            "SonderMasksBridge: torch is unavailable, so latent masks cannot be "
            "emitted. This node's mask outputs require the ComfyUI runtime."
        )


def _no_op_mask():
    """1x1 all-zeros.

    Zero means "keep", so a mask output consumed without its latent returns the
    source unchanged — visibly a non-generation — rather than silently
    regenerating the whole window and ignoring Freeze, which all-ones would do.
    """
    _require_torch()
    return torch.zeros((1, 1, 1), dtype=torch.float32)


def _video_mask_tensor(latent_frames: int, lo: int, hi: int):
    """(T, 1, 1) — one mask per latent frame; the spatial 1s replicate exactly."""
    _require_torch()
    mask = torch.zeros((max(1, latent_frames), 1, 1), dtype=torch.float32)
    if hi > lo:
        mask[lo:hi] = 1.0
    return mask


def _audio_mask_tensor(dim_2: int, dim_3: int, time_axis: int, lo: int, hi: int):
    """(1, d2, d3) — ONE image; a batch never reaches a 4D latent's time axis."""
    _require_torch()
    mask = torch.zeros((1, max(1, dim_2), max(1, dim_3)), dtype=torch.float32)
    if hi > lo:
        if time_axis == -2:
            mask[:, lo:hi, :] = 1.0
        else:
            mask[:, :, lo:hi] = 1.0
    return mask


class SonderMasksBridge:
    """Gate the editor's generation-mask window into per-channel mask times."""

    CATEGORY = "Sonder"
    RETURN_TYPES = ("FLOAT", "FLOAT", "FLOAT", "FLOAT", "MASK", "MASK")
    RETURN_NAMES = (
        "video_mask_start_time",
        "video_mask_end_time",
        "audio_mask_start_time",
        "audio_mask_end_time",
        "video_mask",
        "audio_mask",
    )
    OUTPUT_TOOLTIPS = (
        "Video generation-mask start time (seconds). Equals the editor's mask "
        "start when Edit Video is on; equals the end (zero width) when frozen.",
        "Video generation-mask end time (seconds). Collapses to the start time "
        "when Edit Video is off, freezing the video channel.",
        "Audio generation-mask start time (seconds). Equals the editor's mask "
        "start when Edit Audio is on; equals the end (zero width) when frozen.",
        "Audio generation-mask end time (seconds). Collapses to the start time "
        "when Edit Audio is off, freezing the audio channel.",
        "Hard 0/1 noise mask for the VIDEO latent — one mask per latent frame "
        "(1 = generate, 0 = keep). Feed Set Latent Noise Mask on the video "
        "latent. Not interchangeable with audio_mask. All-zeros when the video "
        "latent and VAE are not both wired, or when Edit Video is off. "
        "On LTX, a graph that also uses guides or a start image should drive "
        "LTXVAudioVideoMask from the time outputs above instead of feeding this "
        "mask to Set Latent Noise Mask — see docs/generating.md. A mask also "
        "replaces rather than composes, so it overwrites any pin a start-image "
        "or continuation node set upstream.",
        "Hard 0/1 noise mask for the AUDIO latent — a SINGLE mask image shaped "
        "to the audio latent's last two axes, not a batch (1 = generate, 0 = "
        "keep). Feed Set Latent Noise Mask on the audio latent. Not "
        "interchangeable with video_mask. All-zeros when the audio latent and "
        "VAE are not both wired, or when Edit Audio is off. Like the video "
        "mask it replaces rather than composes, so it overwrites any noise "
        "mask set upstream.",
    )
    FUNCTION = "execute"
    DESCRIPTION = (
        "Reads the Sonder Editor's generation-mask window and exposes it as "
        "separate video and audio mask-time pairs, each gated by an Edit/Freeze "
        "toggle. A frozen channel emits a zero-width window (start == end) so "
        "nothing is generated for it — useful to keep audio fixed while it drives "
        "video, or vice versa. Wire the times into a downstream temporal mask "
        "node, or wire each latent and its VAE here and take the compiled MASK "
        "outputs straight to Set Latent Noise Mask: Separate AV Latent -> this "
        "bridge -> two Set Latent Noise Mask -> Concat AV Latent. Requires a "
        "project that executed through a Sonder Editor."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "project": ("SONDER_PROJECT", {
                    "tooltip": "Wire from the Sonder Editor project output so the "
                               "bridge sees the same execution window.",
                }),
                "edit_video": ("BOOLEAN", {
                    "default": True,
                    "label_on": "Edit",
                    "label_off": "Freeze",
                    "tooltip": "Edit: video generates over the mask window. "
                               "Freeze: zero-width window keeps video from source.",
                }),
                "edit_audio": ("BOOLEAN", {
                    "default": True,
                    "label_on": "Edit",
                    "label_off": "Freeze",
                    "tooltip": "Edit: audio generates over the mask window. "
                               "Freeze: zero-width window keeps audio from source.",
                }),
            },
            "optional": {
                "video_latent": ("LATENT", {
                    "tooltip": "Optional. Separate AV Latent > video_latent. Sizes "
                               "video_mask and is checked against this render "
                               "window. Needs video_vae wired too.",
                }),
                "audio_latent": ("LATENT", {
                    "tooltip": "Optional. Separate AV Latent > audio_latent. Shapes "
                               "audio_mask and identifies its time axis. Needs "
                               "audio_vae wired too.",
                }),
                "video_vae": ("VAE", {
                    "tooltip": "Optional. The VAE that encoded the video latent — "
                               "supplies the exact pixel-frame to latent-frame map.",
                }),
                "audio_vae": ("VAE", {
                    "tooltip": "Optional. The VAE that encoded the audio latent — "
                               "supplies its latent rate (LTX 25/s, MiniMax H3 40/s).",
                }),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    def _compile_video(self, result, video_latent, video_vae):
        """(mask, provenance) for the video stream."""
        if video_latent is None and video_vae is None:
            return _no_op_mask(), None
        if video_latent is None or video_vae is None:
            logger.warning(
                "masks bridge: video_mask needs BOTH video_latent and video_vae "
                "(latent=%s, vae=%s); emitting a keep-everything mask.",
                video_latent is not None, video_vae is not None,
            )
            return _no_op_mask(), None

        shape = _latent_shape(video_latent, "video_latent", 5)
        latent_frames = shape[2]
        start_pixel, end_pixel = result["video_mask_frames"]
        lo, hi = resolve_video_mask_span(
            result["frame_count"], latent_frames, start_pixel, end_pixel,
            video_downscale_fn(video_vae),
        )
        return _video_mask_tensor(latent_frames, lo, hi), {
            "latent_frames": latent_frames,
            "pixel_span": [int(start_pixel), int(end_pixel)],
            "latent_span": [int(lo), int(hi)],
            "frame_count": int(result["frame_count"]),
        }

    def _compile_audio(self, result, audio_latent, audio_vae):
        """(mask, provenance) for the audio stream."""
        if audio_latent is None and audio_vae is None:
            return _no_op_mask(), None
        if audio_latent is None or audio_vae is None:
            logger.warning(
                "masks bridge: audio_mask needs BOTH audio_latent and audio_vae "
                "(latent=%s, vae=%s); emitting a keep-everything mask.",
                audio_latent is not None, audio_vae is not None,
            )
            return _no_op_mask(), None

        shape = _latent_shape(audio_latent, "audio_latent", 4)
        frame_count = result["frame_count"]
        start_pixel, end_pixel = result["audio_mask_frames"]
        if frame_count <= 0:
            raise RuntimeError(
                "SonderMasksBridge: the render context carries no `frame_count`, "
                "so an audio latent mask cannot be sized."
            )
        if end_pixel <= start_pixel:
            # Frozen, or an empty window: the mask is all-zeros whichever axis
            # is time, so the axis is not resolved at all. On a short clip the
            # fixed frequency/stereo axis can collide with the time axis, and
            # that must not block a render which generates no audio anyway.
            return _audio_mask_tensor(shape[-2], shape[-1], -1, 0, 0), {
                "shape": [int(shape[-2]), int(shape[-1])],
                "time_axis": None,
                "frozen": True,
                "pixel_span": [int(start_pixel), int(end_pixel)],
                "latent_span": [0, 0],
            }

        fps = result["fps"]
        rate = audio_latents_per_second(audio_vae)
        duration = (frame_count / fps) if fps > 0 else 0.0
        expected = int(round(duration * rate))
        time_axis = detect_audio_time_axis(shape, expected)
        axis_len = shape[time_axis]
        lo, hi = resolve_audio_mask_span(frame_count, axis_len, start_pixel, end_pixel)
        return _audio_mask_tensor(shape[-2], shape[-1], time_axis, lo, hi), {
            "shape": [int(shape[-2]), int(shape[-1])],
            "time_axis": int(time_axis),
            "latents_per_second": round(float(rate), 4),
            "expected_length": expected,
            "pixel_span": [int(start_pixel), int(end_pixel)],
            "latent_span": [int(lo), int(hi)],
        }

    def execute(self, project, edit_video=True, edit_audio=True, video_latent=None,
                audio_latent=None, video_vae=None, audio_vae=None, unique_id=None):
        result = resolve_mask_times(project, edit_video, edit_audio)

        video_mask, video_prov = self._compile_video(result, video_latent, video_vae)
        audio_mask, audio_prov = self._compile_audio(result, audio_latent, audio_vae)

        # Provenance: public (non-underscore) ctx keys auto-flow into
        # take/asset generation_params via _public_execution_context at save
        # time, so every value here must stay JSON-safe — never a tensor.
        payload = {
            "edit_video": result["edit_video"],
            "edit_audio": result["edit_audio"],
            "video_mask": [result["video_mask_start_time"], result["video_mask_end_time"]],
            "audio_mask": [result["audio_mask_start_time"], result["audio_mask_end_time"]],
            "video_latent_mask": video_prov,
            "audio_latent_mask": audio_prov,
            # Provenance only - pinned padding is a correct, common configuration, so this
            # is never a warning. Derive it from the emitted window rather than the Edit/Freeze
            # switch: the pad sits at the tensor tail, AFTER post-context, while mask_end_pixel
            # adds the padding before it, so an Edit channel with any post-context also leaves
            # the pad outside its mask. Freeze is only the zero-width special case.
            "frame_count_padding": result["frame_count_padding"],
            "video_pins_padding": _pins_padding(result, result["video_mask_frames"]),
            "audio_pins_padding": _pins_padding(result, result["audio_mask_frames"]),
        }
        ctx = getattr(project, "_execution_context", None)
        if isinstance(ctx, dict):
            # `masks_bridge` keeps its established last-writer shape; the
            # by-node map is additive so two bridges in one graph cannot erase
            # each other's record, which is the diagnostic route for a mask that
            # landed somewhere unexpected.
            ctx["masks_bridge"] = payload
            by_node = ctx.get("masks_bridge_by_node")
            if not isinstance(by_node, dict):
                by_node = {}
            by_node[str(unique_id) if unique_id is not None else "0"] = payload
            ctx["masks_bridge_by_node"] = by_node

        logger.info(
            "masks bridge: edit_video=%s edit_audio=%s video=[%.4f,%.4f] audio=[%.4f,%.4f] "
            "padding=%d",
            result["edit_video"],
            result["edit_audio"],
            result["video_mask_start_time"],
            result["video_mask_end_time"],
            result["audio_mask_start_time"],
            result["audio_mask_end_time"],
            result["frame_count_padding"],
        )
        if video_prov or audio_prov:
            logger.info("masks bridge latent masks: video=%s audio=%s", video_prov, audio_prov)

        return (
            result["video_mask_start_time"],
            result["video_mask_end_time"],
            result["audio_mask_start_time"],
            result["audio_mask_end_time"],
            video_mask,
            audio_mask,
        )
