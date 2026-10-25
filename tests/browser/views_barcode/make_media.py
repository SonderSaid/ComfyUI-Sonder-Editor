"""Barcode media for the Output Views linked-playback fixture (Phase 3B).

Every video frame paints its own index as 16 black/white stripes (MSB first),
so a pixel read of any presented frame says exactly which source frame it is.
Each file carries a tone at its own frequency, so an analyser can tell which
element is audible. Writes H.264/AAC MP4s (Chrome decodes them) plus a WAV for
the timeline audio track.

Run with the test install's Python (it has PyAV):
    "<venv>/Scripts/python.exe" -I tests/browser/views_barcode/make_media.py
"""

from __future__ import annotations

import math
import sys
from fractions import Fraction
from pathlib import Path

import av
import numpy as np

HERE = Path(__file__).resolve().parent
MEDIA = HERE / "media"
WIDTH, HEIGHT, BITS = 320, 64, 16
SAMPLE_RATE = 48000

# name -> (fps, frames, tone Hz). The scene runs 120 frames at 24 fps; each
# output covers it at its own rate (authored timing, offset 0).
VIDEOS = {
    "composite.mp4": (24, 120, 330),
    "out12.mp4": (12, 64, 660),
    "out25.mp4": (25, 130, 880),
    "out30.mp4": (30, 156, 1100),
}
TIMELINE_WAV = ("timeline.wav", 120 / 24, 220)


def barcode(index: int) -> np.ndarray:
    frame = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    stripe = WIDTH // BITS
    for bit in range(BITS):
        if index >> (BITS - 1 - bit) & 1:
            frame[:, bit * stripe:(bit + 1) * stripe, :] = 255
    return frame


def tone(seconds: float, hz: float) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (0.4 * np.sin(2 * math.pi * hz * t)).astype(np.float32)


def write_video(path: Path, fps: int, frames: int, hz: float) -> None:
    with av.open(str(path), "w") as container:
        video = container.add_stream("libx264", rate=Fraction(fps, 1))
        video.width, video.height, video.pix_fmt = WIDTH, HEIGHT, "yuv420p"
        video.options = {"crf": "8", "g": "12", "bf": "0"}
        audio = container.add_stream("aac", rate=SAMPLE_RATE)
        audio.layout = "mono"
        for index in range(frames):
            for packet in video.encode(av.VideoFrame.from_ndarray(barcode(index), format="rgb24")):
                container.mux(packet)
        for packet in video.encode():
            container.mux(packet)
        samples = tone(frames / fps, hz)
        chunk = 1024
        for start in range(0, len(samples), chunk):
            part = samples[start:start + chunk]
            frame = av.AudioFrame.from_ndarray(part.reshape(1, -1), format="flt", layout="mono")
            frame.sample_rate = SAMPLE_RATE
            frame.pts = start
            for packet in audio.encode(frame):
                container.mux(packet)
        for packet in audio.encode():
            container.mux(packet)


def write_wav(path: Path, seconds: float, hz: float) -> None:
    with av.open(str(path), "w", format="wav") as container:
        stream = container.add_stream("pcm_s16le", rate=SAMPLE_RATE)
        stream.layout = "mono"
        pcm = (tone(seconds, hz) * 32767).astype(np.int16)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = SAMPLE_RATE
        for packet in stream.encode(frame):
            container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def main() -> int:
    MEDIA.mkdir(exist_ok=True)
    for name, (fps, frames, hz) in VIDEOS.items():
        write_video(MEDIA / name, fps, frames, hz)
    name, seconds, hz = TIMELINE_WAV
    write_wav(MEDIA / name, seconds, hz)
    print("wrote", ", ".join(sorted(p.name for p in MEDIA.iterdir())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
