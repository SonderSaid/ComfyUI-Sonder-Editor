"""What a model's VAE says about its Model Template values (the Sonder Template Helper's core).

ComfyUI's VAE encode cuts audio to whole ``downscale_ratio`` blocks at the encoder's rate
(``comfy.sd.VAE.vae_encode_crop_pixels``). A template's **Audio grid** is that rate and
block. This module reads them from the VAE object ComfyUI loaded and checks them against
the VAE's own crop method, so a user can set up a template for a model whose audio
encoder has no built-in grid.

``audio_encoder_facts`` is the one reading of a VAE's load-time audio attributes; the
Masks Bridge's grid check (``nodes/masks_bridge.py``) uses it too. The crop check runs
only in the helper: small CPU ramps through the crop, so the model is never loaded onto a
device or run. Pure Python apart from a lazy ``torch`` import, so it is testable without
ComfyUI.
"""

from __future__ import annotations

import numbers

from ..server.audio_grid import valid_audio_grid

# Where the values go (docs/generating.md, Model templates; editor_settings_panel.js labels).
WHERE = ("In the Sonder Editor's Settings ▸ Model Templates, edit the template: set Audio grid "
         "to Custom and enter them in Audio rate / block.")
BUILT_IN = "LTX 2.3 / 2.5 and MiniMax H3 templates have their grid built in (Audio grid: Built-in)."
NOT_MEASURED = "Not checked: timing inside the encoder itself."
# LTX's audio encoder (comfy.ldm.lightricks) has a known offset of its own (bug tracker).
LTX_NOTE = " LTX audio has a separate offset of about 22 ms that no grid removes."


def _scalar(value) -> bool:
    """A real number (a block), not a bool, tuple or per-axis formula."""
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _whole(value):
    """``value`` as an int when it is integral, else ``value`` unchanged."""
    if isinstance(value, numbers.Integral) and not isinstance(value, bool):
        return int(value)
    if _scalar(value) and float(value).is_integer():
        return int(value)
    return value


def _samples(count: int) -> str:
    return f"{count} sample" if count == 1 else f"{count} samples"


def audio_encoder_facts(vae) -> dict:
    """The load-time audio attributes ComfyUI sets on a VAE (``comfy/sd.py``); never raises.

    ``{"audio", "rate", "block", "crops"}``: ``audio`` is True for a stereo audio layout
    (a scalar ``downscale_ratio`` and two output channels: every core audio VAE, and no
    image or video VAE); ``rate`` and ``block`` are ints when integral, else as found
    (``None`` when absent or not a number); ``crops`` is ``crop_input`` when a bool.
    """
    ratio = getattr(vae, "downscale_ratio", None)
    rate = _whole(getattr(vae, "audio_sample_rate", None))
    crops = getattr(vae, "crop_input", None)
    return {
        "audio": _scalar(ratio) and getattr(vae, "output_channels", None) == 2,
        "rate": rate if _scalar(rate) else None,
        "block": _whole(ratio) if _scalar(ratio) else None,
        "crops": crops if isinstance(crops, bool) else None,
    }


def _result(status: str, lines: list[str], rate: int = 0, block: int = 0) -> dict:
    return {"status": status, "rate": rate, "block": block, "lines": lines}


def _off(status: str, kind: str, *reason: str) -> dict:
    return _result(status, [f"{kind}: no audio grid (outputs 0, 0).", *reason])


def _kind(vae) -> str:
    model = getattr(vae, "first_stage_model", None)
    return type(model).__name__ if model is not None else "VAE"


def _crop_rows(vae, block: int) -> list[dict]:
    """Ramps of ``k·block + extra`` samples through the VAE's own crop, CPU only.

    Each row says whether the crop kept exactly the whole blocks as one contiguous run
    (``front_cut``: where that run starts; None when the content is not a run).
    """
    import torch

    rows = []
    for k in (1, 2):
        for extra in (0, block // 2, block - 1):
            length = k * block + extra
            ramp = torch.arange(length, dtype=torch.float32).reshape(1, length, 1).repeat(1, 1, 2)
            kept = vae.vae_encode_crop_pixels(ramp)
            survived = int(kept.shape[1])
            front = None
            if 0 < survived <= length:
                first = int(round(float(kept[0, 0, 0])))
                if (0 <= first <= length - survived
                        and torch.equal(kept[0, :, 0], ramp[0, first:first + survived, 0])):
                    front = first
            rows.append({"length": length, "extra": extra, "kept": survived, "front_cut": front,
                         "whole_blocks": survived == k * block and front is not None})
    return rows


def audio_grid_from_vae(vae) -> dict:
    """The Audio grid ``vae`` needs: ``{"status", "rate", "block", "lines"}``.

    ``rate`` and ``block`` are 0 unless a template should use a grid; ``lines`` is the
    report. Statuses: ``grid``; ``no_vae``, ``not_audio``, ``no_encoder``, ``no_crop``,
    ``unsupported``, ``crop_unexpected`` (each with its reason).
    """
    if vae is None or getattr(vae, "first_stage_model", None) is None:
        return _off("no_vae", "Input", "This is not a loaded VAE. Wire the model's audio VAE.")
    kind = _kind(vae)
    facts = audio_encoder_facts(vae)
    if not facts["audio"]:
        return _off("not_audio", kind,
                    "This is not a stereo audio VAE (image and video VAEs land here). Wire the model's audio VAE.")
    encode = getattr(vae.first_stage_model, "encode", None)
    estimate = getattr(vae, "memory_used_encode", None)
    refusal = None if callable(encode) else "it has no encoder"
    if refusal is None and callable(estimate):
        try:
            import torch
            estimate((1, 2, max(1, int(facts["block"]))), torch.float32)
        except Exception as exc:  # a decoder-only VAE refuses here (MiniMax Music3 DAV)
            refusal = str(exc) or type(exc).__name__
    if refusal is not None:
        return _off("no_encoder", kind,
                    f"This VAE cannot encode ({refusal}), so it never crops audio. Set the Audio grid to Off.")
    rate, block = facts["rate"], facts["block"]
    values = f"rate {rate} Hz, block {block} samples"
    if facts["crops"] is False:
        return _off("no_crop", kind, f"Its encoder ({values}) does not crop its input, so audio of any "
                                     "length passes whole. Set the Audio grid to Off.")
    if valid_audio_grid({"rate": rate, "block": block}) is None:
        return _off("unsupported", kind,
                    f"Its encoder uses {values}. An Audio grid needs a standard rate and a whole block "
                    "from 2 up to the rate, so this cannot be entered.")
    try:
        rows = _crop_rows(vae, block)
    except Exception as exc:  # a VAE-like object without ComfyUI's crop: report, never fail the node
        return _off("crop_unexpected", kind,
                    f"Its crop could not be checked ({exc or type(exc).__name__}). Set the Audio grid to Off.")
    whole = [row for row in rows if row["extra"] == 0]
    if not all(row["whole_blocks"] and row["front_cut"] == 0 for row in whole):
        row = next(row for row in whole if not (row["whole_blocks"] and row["front_cut"] == 0))
        return _off("crop_unexpected", kind,
                    f"Its crop changed a whole number of blocks ({row['kept']} of {row['length']} samples kept), "
                    "so padding to blocks would not help. Set the Audio grid to Off.")
    others = [row for row in rows if row["extra"]]
    if all(row["kept"] == row["length"] for row in others):
        return _off("no_crop", kind, f"Its encoder ({values}) keeps audio of any length whole, so it needs "
                                     "no grid. Set the Audio grid to Off.")
    odd = next((row for row in others if not row["whole_blocks"]), None)
    if odd is not None:
        return _off("crop_unexpected", kind,
                    f"Its crop kept {odd['kept']} of {odd['length']} samples, not the whole blocks, so a grid "
                    "cannot be trusted with it. Set the Audio grid to Off.")
    lost = max(row["length"] - row["kept"] for row in others)
    front = max(row["front_cut"] for row in others)
    return _result("grid", [
        f"{kind}: Audio grid {rate} Hz, block {_samples(block)} ({1000 * block / rate:.1f} ms).",
        WHERE,
        BUILT_IN,
        f"Checked with this VAE's own crop: whole blocks pass intact; other lengths lose up to "
        f"{_samples(lost)}, up to {front} ({1000 * front / rate:.1f} ms) of them from the start.",
        NOT_MEASURED + (LTX_NOTE if "lightricks" in type(vae.first_stage_model).__module__ else ""),
    ], rate=rate, block=block)
