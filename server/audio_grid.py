"""The audio grid: the audio encoder's sample rate and crop block, declared by a Model Template.

ComfyUI's VAE encode cuts an audio waveform, at the encoder's rate, to a whole number of
`downscale_ratio` blocks, half of the excess from the front (`comfy.sd.VAE.vae_encode_crop_pixels`).
Encoded audio therefore starts up to half a block late. Audio served at the encoder's rate
with a whole number of blocks loses nothing.

The browser owns the template registry and projects the resolved `{rate, block}` onto the
project (`audio_constraint`) and each queued job, as it does the frame rule. This module only
validates the value a run uses; stored values stay raw so a newer format is never rewritten.
It is pure (no torch, no numpy) so any module can import it.
"""

from __future__ import annotations

from fractions import Fraction

# The rates a template may declare. Mirrors `AUDIO_GRID_RATES` in web/js/editor_settings.js
# (parity test: tests/test_audio_grid.py). Standard rates only: the mixer's resampler and
# ComfyUI's sinc resampler both cost more as the two rates' common divisor shrinks.
AUDIO_GRID_RATES = (8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000, 88200, 96000)


def valid_audio_grid(value) -> dict | None:
    """`{"rate": int, "block": int}` when `value` holds a supported grid, else None.

    Exact ints only (a bool, float or string is not a grid), a rate from
    `AUDIO_GRID_RATES` and `2 <= block <= rate`. Extra keys are ignored, not rejected,
    so a value written by a newer version still yields its grid.
    """
    if not isinstance(value, dict):
        return None
    rate, block = value.get("rate"), value.get("block")
    if type(rate) is not int or rate not in AUDIO_GRID_RATES:
        return None
    if type(block) is not int or not 2 <= block <= rate:
        return None
    return {"rate": rate, "block": block}


def frame_sample(frame, rate, fps):
    """Half-up rounding on the clock whose origin is scene frame zero.

    The scene audio mixer's own clock (`server/audio_pipeline.py` imports it from here),
    so a grid plan and the mix it describes cannot disagree by a sample.
    """
    value = Fraction(int(frame) * int(rate), 1) / Fraction(str(fps))
    return (2 * value.numerator + value.denominator) // (2 * value.denominator)


def audio_grid_plan(start, end, fps, grid, frame_target=0):
    """The Editor's audio length for the window `[start, end)` on `grid`, before any mix.

    `{"rate", "block", "mix_len", "frame_len", "total_len", "grid_padding"}`:
    - `mix_len`: what the mixer returns at `rate` (its own clock, `frame_sample`).
    - `frame_len`: 0 when frame padding is off (`frame_target` 0), else the padded
      frame count on the same clock, so one sample of clock mismatch can never add a block.
    - `total_len`: `max(mix_len, frame_len)` rounded up to whole blocks; the Editor pads
      (mirrored, never trimmed) to exactly this.
    - `grid_padding`: the samples the grid adds beyond `max(mix_len, frame_len)`.
    Pure arithmetic, so the execution context can publish it before a deferred mix runs.
    """
    rate, block = int(grid["rate"]), int(grid["block"])
    origin = frame_sample(start, rate, fps)
    mix_len = max(0, frame_sample(end, rate, fps) - origin)
    target = max(0, int(frame_target or 0))
    frame_len = max(0, frame_sample(int(start) + target, rate, fps) - origin) if target else 0
    needed = max(mix_len, frame_len)
    total_len = -(-needed // block) * block
    return {"rate": rate, "block": block, "mix_len": mix_len, "frame_len": frame_len,
            "total_len": total_len, "grid_padding": total_len - needed}
