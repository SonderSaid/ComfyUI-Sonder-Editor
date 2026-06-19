"""Tests for shared media helpers."""

import os
import sys
import types
import wave

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server.media_helpers import (  # noqa: E402
    DEFAULT_CROP_POSITION,
    DEFAULT_FIT_MODE,
    _resize_interpolation,
    decode_audio_samples,
    fit_frame_to_canvas,
    write_audio_wav,
)
import server.media_helpers as media_helpers  # noqa: E402


def _portrait(w=2, h=4, value=255):
    return np.full((h, w, 3), value, dtype=np.uint8)


def test_default_mode_is_pad_edge():
    assert DEFAULT_FIT_MODE == "pad_edge"
    assert DEFAULT_CROP_POSITION == "center"


def test_fit_mode_letterboxes_with_black_bars_and_inner_bounds():
    # 2x4 portrait into 4x4 → scale 1, centered at x_off=1; bars are black,
    # and the returned bounds are the INNER content rect (so lower layers show through).
    canvas, (x, y, w, h) = fit_frame_to_canvas(_portrait(), 4, 4, mode="fit")
    assert (x, y, w, h) == (1, 0, 2, 4)
    assert np.all(canvas[:, 0, :] == 0)   # left bar black
    assert np.all(canvas[:, 3, :] == 0)   # right bar black
    assert np.all(canvas[:, 1:3, :] == 255)


def test_pad_edge_replicates_edges_no_black_and_full_bounds():
    # Same geometry, but bars are edge-replicated → no black; bounds = full canvas.
    src = _portrait(value=255)
    canvas, bounds = fit_frame_to_canvas(src, 4, 4, mode="pad_edge")
    assert bounds == (0, 0, 4, 4)
    assert not np.any(canvas == 0)        # no black bars
    assert np.all(canvas == 255)


def test_pad_edge_bar_matches_source_edge_color():
    # Distinct left/right columns so we can prove the bar copies the adjacent edge.
    src = np.zeros((4, 2, 3), dtype=np.uint8)
    src[:, 0, :] = (10, 20, 30)   # left column
    src[:, 1, :] = (200, 210, 220)  # right column
    canvas, _ = fit_frame_to_canvas(src, 4, 4, mode="pad_edge")
    # placed at cols 1-2; left bar (col 0) replicates resized col 0, right bar (col 3) col 1.
    assert np.array_equal(canvas[:, 0, :], canvas[:, 1, :])
    assert np.array_equal(canvas[:, 3, :], canvas[:, 2, :])
    assert not np.array_equal(canvas[:, 0, :], canvas[:, 3, :])


def test_stretch_fills_canvas_exactly():
    canvas, bounds = fit_frame_to_canvas(_portrait(), 4, 4, mode="stretch")
    assert bounds == (0, 0, 4, 4)
    assert canvas.shape == (4, 4, 3)


def test_cover_fills_canvas_and_crop_position_shifts_anchor():
    # Vertical gradient portrait so the crop anchor is observable.
    src = np.zeros((4, 2, 3), dtype=np.uint8)
    for row in range(4):
        src[row, :, :] = row * 60
    center, b_center = fit_frame_to_canvas(src, 4, 4, mode="cover", crop_position="center")
    top, _ = fit_frame_to_canvas(src, 4, 4, mode="cover", crop_position="top")
    bottom, _ = fit_frame_to_canvas(src, 4, 4, mode="cover", crop_position="bottom")
    assert b_center == (0, 0, 4, 4)
    assert center.shape == (4, 4, 3)
    # Different vertical anchors keep the brightest/darkest rows in different places.
    assert int(top[0, 0, 0]) < int(bottom[0, 0, 0])
    assert not np.array_equal(top, bottom)


def test_invalid_mode_and_crop_fall_back_to_constants():
    bogus, bounds = fit_frame_to_canvas(_portrait(), 4, 4, mode="zoom", crop_position="middle")
    pad, pad_bounds = fit_frame_to_canvas(_portrait(), 4, 4, mode="pad_edge")
    assert bounds == pad_bounds
    assert np.array_equal(bogus, pad)


def test_resize_interpolation_picks_area_on_downscale_and_lanczos_on_upscale():
    assert _resize_interpolation(1000, 1000, 500, 500) == cv2.INTER_AREA
    assert _resize_interpolation(500, 500, 1000, 1000) == cv2.INTER_LANCZOS4
    # Equal size is not a downscale → sharp path.
    assert _resize_interpolation(640, 480, 640, 480) == cv2.INTER_LANCZOS4


def test_decode_audio_samples_can_return_channel_first_stereo(monkeypatch):
    pcm = np.array(
        [1000, -1000, 2000, -2000, 3000, -3000, 4000, -4000],
        dtype=np.int16,
    ).tobytes()

    def fake_run(cmd, capture_output, timeout):
        assert "-ac" in cmd
        assert cmd[cmd.index("-ac") + 1] == "2"
        assert "-ar" in cmd
        assert cmd[cmd.index("-ar") + 1] == "48000"
        return types.SimpleNamespace(returncode=0, stdout=pcm, stderr=b"")

    monkeypatch.setattr(media_helpers, "get_ffmpeg_path", lambda: "ffmpeg")
    monkeypatch.setattr(media_helpers.subprocess, "run", fake_run)

    samples, sample_rate = decode_audio_samples(
        "audio.wav",
        sample_rate=48000,
        channels=2,
        mix_to_mono=False,
    )

    expected = np.array(
        [[1000, 2000, 3000, 4000], [-1000, -2000, -3000, -4000]],
        dtype=np.float32,
    ) / 32768.0
    assert sample_rate == 48000
    assert samples.shape == (2, 4)
    assert np.allclose(samples, expected)


def test_write_audio_wav_writes_channel_first_float_pcm(tmp_path):
    output_path = tmp_path / "audio.wav"
    samples = np.array(
        [[0.0, 0.5, -1.0], [1.0, -0.5, 0.25]],
        dtype=np.float32,
    )

    write_audio_wav(str(output_path), samples, 8000)

    with wave.open(str(output_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 2
        assert wav_file.getsampwidth() == 2
        assert wav_file.getframerate() == 8000
        pcm = np.frombuffer(wav_file.readframes(3), dtype="<i2").reshape(-1, 2)

    expected = np.array(
        [[0, 32767], [16383, -16383], [-32767, 8191]],
        dtype=np.int16,
    )
    assert np.array_equal(pcm, expected)
