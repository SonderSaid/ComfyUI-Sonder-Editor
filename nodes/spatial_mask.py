"""Compile a graph-supplied pixel MASK against the Editor's resolved video window."""

from itertools import accumulate
import sys

import torch


_SUPPORTED = {
    ("comfy.ldm.lightricks.vae.causal_video_autoencoder", "VideoVAE"),
    ("comfy.ldm.lightricks.vae.na_diffusion_decoder", "CausalDiffusionVAE"),
    ("comfy.ldm.minimax.vae", "MiniMaxH3VideoVAE"),
}


def _error(message):
    return RuntimeError(f"SonderMasksBridge: {message}")


def _native_layout(vae, shape):
    """Return pixel-frame edges and decoded raster for one separated video.

    decode_output_shape describes allocation. Only use prefix lengths for a
    family whose token coordinates independently establish those boundaries.
    """
    batch, channels, tokens, h, w = shape
    if min(shape) < 1 or batch != 1 or channels != vae.latent_channels:
        raise _error(f"expected one video [1,{vae.latent_channels},T,h,w], got {shape}.")
    model = vae.first_stage_model
    cls = type(model)
    module = cls.__module__
    # The defining module is already loaded with the wired model. Never import
    # another family's optional modules to register the Bridge or recognize it.
    if getattr(sys.modules.get(module), cls.__name__, None) is not cls:
        raise _error(f"unsupported spatial mask VAE {module}.{cls.__name__}.")
    kind = (module, cls.__name__)
    if kind not in _SUPPORTED:
        raise _error(f"unsupported spatial mask VAE {module}.{cls.__name__}. Wire the native "
                     "LTX or MiniMax H3 encoder, or disconnect spatial_mask for temporal masking.")
    # Before any family check, so a decoder-only file names the missing encoder.
    if not hasattr(model, "encoder"):
        raise _error("spatial_mask requires the encoding VAE with its video encoder available.")
    if kind == ("comfy.ldm.lightricks.vae.causal_video_autoencoder", "VideoVAE"):
        decoded = [model.decode_output_shape((batch, channels, k, h, w))
                   for k in range(1, tokens + 1)]
        edges = [0] + [s[2] for s in decoded]
        raster = tuple(decoded[-1][-2:])
        layout = "ltx-convolutional"
    elif kind == ("comfy.ldm.lightricks.vae.na_diffusion_decoder", "CausalDiffusionVAE"):
        decoder = model.decoder
        # Native causal pixel shuffle removes duplicate leading pixels only at
        # temporal origin. Every subsequent token contributes its full stride.
        stride = decoder.temporal_upscale
        edges = [0] + [1 + i * stride for i in range(tokens)]
        raster = (h * decoder.spatial_upscale, w * decoder.spatial_upscale)
        layout = "ltx-causal-diffusion"
    elif kind == ("comfy.ldm.minimax.vae", "MiniMaxH3VideoVAE"):
        from comfy.ldm.minimax.model import FRAME_PER_TOKEN

        if (model.clip_length != sum(FRAME_PER_TOKEN)
                or model.tokens_chunk_size != len(FRAME_PER_TOKEN)
                or model.vae_ratio_t != max(FRAME_PER_TOKEN) or model.token_drop != 3):
            raise _error("unsupported MiniMax H3 encoder timing for spatial_mask.")
        edges = [0, *accumulate(FRAME_PER_TOKEN[i % len(FRAME_PER_TOKEN)] for i in range(tokens))]
        raster = (h * model.vae_ratio, w * model.vae_ratio)
        if hasattr(model, "decoder") and tuple(model.decode_output_shape(shape)[2:]) != (edges[-1], *raster):
            raise _error("MiniMax H3 decoder geometry disagrees with its video token timing.")
        layout = "minimax-h3"
    wrapper_raster = (h * vae.downscale_ratio[1], w * vae.downscale_ratio[2])
    if raster != wrapper_raster:
        raise _error(f"VAE decoder raster {raster} disagrees with encoder raster {wrapper_raster}.")
    return layout, edges, raster


def _check_tensor(mask, name):
    if (not isinstance(mask, torch.Tensor) or mask.layout != torch.strided or mask.is_nested
            or not (mask.is_floating_point() or mask.dtype == torch.bool)):
        raise _error(f"{name} must be a dense boolean or floating tensor with values in [0,1].")


def _in_range(mask):
    # NaN propagates through aminmax, so either comparison rejects it. Bool
    # tensors already have the required range.
    if mask.dtype == torch.bool:
        return torch.ones((), dtype=torch.bool, device=mask.device)
    low, high = torch.aminmax(mask)
    return (low >= 0) & (high <= 1)


def _upstream_plane(mask, shape):
    _check_tensor(mask, "noise_mask")
    _, c, t, h, w = shape
    original = tuple(mask.shape)
    if mask.ndim == 5 and mask.shape[0] == 1 and mask.shape[1] in (1, c):
        if mask.shape[1] > 1 and not torch.equal(mask[:, :1].expand_as(mask), mask):
            raise _error("noise_mask has unequal channel pins that one video MASK cannot represent.")
        mask = mask[0, 0]
    elif mask.ndim == 4 and mask.shape[1] == 1:
        mask = mask[:, 0]
    elif mask.ndim == 2:
        mask = mask.unsqueeze(0)
    if mask.ndim != 3 or any(n not in (1, target) for n, target in zip(mask.shape, (t, h, w))):
        raise _error(f"noise_mask {original} cannot represent one [T,H,W] mask for {(t,h,w)} "
                     "using only singleton broadcasting.")
    if not _in_range(mask).item():
        raise _error("noise_mask must contain finite values in [0,1].")
    return mask


def _cell_mean(plane, grid):
    """Mean of each latent cell, so partial coverage becomes grey.

    The raster is a whole multiple of the latent grid, so cells tile it
    exactly. Bilinear resizing would read only each cell's centre pixels.
    Float64 keeps a uniform cell exactly its value (MPS lacks float64).
    """
    h, w = grid
    if plane.device.type == "mps":
        plane = plane.cpu()
    cells = plane.to(torch.float64).reshape(h, plane.shape[0] // h, w, plane.shape[1] // w)
    return cells.mean(dim=(1, 3)).to(torch.float32)


@torch.no_grad()
def compile_spatial_mask(spatial, latent, vae, editor_raster, temporal, provenance):
    shape = tuple(latent["samples"].shape)
    if not hasattr(vae, "latent_channels") or not hasattr(vae, "first_stage_model"):
        raise _error("spatial_mask requires a native LTX or MiniMax H3 encoding video VAE.")
    layout, edges, raster = _native_layout(vae, shape)
    frame_count = provenance["frame_count"]
    if edges[-1] != frame_count or any(b <= a for a, b in zip(edges, edges[1:])):
        raise _error(f"{layout} token edges for {shape[2]} latents cover {edges[-1]} frames; "
                     f"expected {frame_count} frames in nonempty intervals.")
    lo, hi = provenance["latent_span"]
    start, end = provenance["pixel_span"]
    if start < end and (edges[lo], edges[hi]) != (start, end):
        raise _error(f"Edit window [{start},{end}) is off the {layout} grid. "
                     f"The resolved latent span covers [{edges[lo]},{edges[hi]}).")
    _check_tensor(spatial, "spatial_mask")
    if editor_raster is None or tuple(editor_raster) != raster:
        raise _error(f"expected Editor render raster {editor_raster} to match encoded raster "
                     f"{raster}. Wire this window's uncropped video latent and its encoder.")
    expected = (frame_count, *raster)
    if tuple(spatial.shape) != expected:
        raise _error(f"expected spatial_mask {expected}, got {tuple(spatial.shape)}. "
                     "Include the entire render window, context and padding.")
    stored = latent.get("noise_mask")
    upper = None if stored is None else _upstream_plane(stored, shape)
    output = spatial.new_zeros(shape[2:], dtype=torch.float32)
    valid = torch.ones((), dtype=torch.bool, device=spatial.device)
    for i, (a, b) in enumerate(zip(edges, edges[1:])):
        group = spatial[a:b]
        valid &= _in_range(group)
        if lo <= i < hi:
            output[i] = _cell_mean(group.amax(dim=0), shape[-2:])
    if not valid.item():
        raise _error("spatial_mask must contain finite values in [0,1], including protected frames.")
    output.mul_(temporal.to(output.device))
    if upper is not None:
        torch.minimum(output, upper.to(output), out=output)
    return output, {"layout": layout, "input_shape": list(expected), "output_shape": list(output.shape),
                    "pooling": "temporal_max_then_area", "upstream_minimum": upper is not None,
                    "upstream_shape": list(stored.shape) if stored is not None else None}
