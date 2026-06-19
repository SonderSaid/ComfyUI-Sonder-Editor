from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from typing import Iterable, Iterator

import cv2
import numpy as np

logger = logging.getLogger("sonder_editor")


class MediaOperationCancelled(RuntimeError):
    """Raised when a cooperative media operation is cancelled."""


class MediaProbeError(RuntimeError):
    """Raised when media metadata cannot be probed well enough to register safely."""


DEFAULT_SAVE_VIDEO_PRESET = "Compatible MP4"
CUSTOM_SAVE_VIDEO_PRESET = "Custom"
CUSTOM_OUTPUT_KIND_VIDEO = "Video File"
CUSTOM_OUTPUT_KIND_PNG_SEQUENCE = "PNG Sequence"
CUSTOM_OUTPUT_KIND_OPTIONS = [CUSTOM_OUTPUT_KIND_VIDEO, CUSTOM_OUTPUT_KIND_PNG_SEQUENCE]
CUSTOM_CONTAINER_OPTIONS = ["mp4", "mov", "mkv"]
CUSTOM_VIDEO_CODEC_OPTIONS = ["libx264", "libx265", "prores_ks", "ffv1"]
CUSTOM_PIX_FMT_OPTIONS = ["yuv420p", "yuv444p", "yuv422p10le", "gbrp"]
CUSTOM_ENCODER_PRESET_OPTIONS = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
]
CUSTOM_AUDIO_CODEC_OPTIONS = ["aac", "pcm_s16le", "flac", "none"]
DEFAULT_TENSOR_MODE = "round"
MIN_SAVE_VIDEO_ENCODE_TIMEOUT_SECONDS = 90
MAX_SAVE_VIDEO_ENCODE_TIMEOUT_SECONDS = 60 * 60

_PRESET_TIMEOUT_SECONDS_PER_MEGAPIXEL_FRAME = {
    "Compatible MP4": 0.35,
    "High Quality MP4": 0.5,
    "Editing Master MP4": 1.5,
    "ProRes 422 HQ": 0.6,
    "Lossless FFV1 (RGB)": 1.0,
}

_CUSTOM_ENCODER_PRESET_TIMEOUT_MULTIPLIER = {
    "ultrafast": 0.2,
    "superfast": 0.25,
    "veryfast": 0.35,
    "faster": 0.5,
    "fast": 0.65,
    "medium": 0.85,
    "slow": 1.0,
    "slower": 1.25,
    "veryslow": 1.5,
}

_CUSTOM_CODEC_TIMEOUT_BASE = {
    "libx264": 0.55,
    "libx265": 0.9,
    "prores_ks": 0.6,
    "ffv1": 1.0,
}

_PIX_FMT_TIMEOUT_MULTIPLIER = {
    "yuv420p": 1.0,
    "yuv444p": 1.25,
    "yuv422p10le": 1.2,
    "gbrp": 1.3,
}

SAVE_VIDEO_PRESET_ORDER = [
    "Compatible MP4",
    "High Quality MP4",
    "Editing Master MP4",
    "ProRes 422 HQ",
    "Lossless FFV1 (RGB)",
    CUSTOM_SAVE_VIDEO_PRESET,
]

SAVE_VIDEO_PRESETS = {
    "Compatible MP4": {
        "extension": ".mp4",
        "tensor_mode": DEFAULT_TENSOR_MODE,
        "video_args": ["-c:v", "libx264", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        "audio_args": ["-c:a", "aac", "-b:a", "192k"],
        "codec": "libx264",
        "pix_fmt": "yuv420p",
        "browser_preview_compatible": True,
        "description": "Browser-safe MP4 for everyday review and sharing.",
    },
    "High Quality MP4": {
        "extension": ".mp4",
        "tensor_mode": DEFAULT_TENSOR_MODE,
        "video_args": ["-c:v", "libx264", "-preset", "slow", "-crf", "14", "-pix_fmt", "yuv420p", "-movflags", "+faststart"],
        "audio_args": ["-c:a", "aac", "-b:a", "256k"],
        "codec": "libx264",
        "pix_fmt": "yuv420p",
        "browser_preview_compatible": True,
        "description": "Browser-safe MP4 with higher visual quality and larger files.",
    },
    "Editing Master MP4": {
        "extension": ".mp4",
        "tensor_mode": "round",
        "video_args": ["-c:v", "libx264", "-preset", "veryslow", "-crf", "10", "-pix_fmt", "yuv444p", "-movflags", "+faststart"],
        "audio_args": ["-c:a", "aac", "-b:a", "256k"],
        "codec": "libx264",
        "pix_fmt": "yuv444p",
        "browser_preview_compatible": False,
        "description": "High-fidelity 4:4:4 MP4 for internal round trips; browser preview may not decode it.",
    },
    "ProRes 422 HQ": {
        "extension": ".mov",
        "tensor_mode": "round",
        "video_args": ["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-vendor", "apl0", "-bits_per_mb", "8000"],
        "audio_args": ["-c:a", "pcm_s16le"],
        "codec": "prores_ks",
        "pix_fmt": "yuv422p10le",
        "browser_preview_compatible": False,
        "description": "Large editing handoff file with 10-bit ProRes video and PCM audio.",
    },
    "Lossless FFV1 (RGB)": {
        "extension": ".mkv",
        "tensor_mode": "round",
        "video_args": ["-c:v", "ffv1", "-level", "3", "-coder", "1", "-context", "1", "-g", "1", "-slices", "24", "-slicecrc", "1", "-pix_fmt", "gbrp"],
        "audio_args": ["-c:a", "flac"],
        "codec": "ffv1",
        "pix_fmt": "gbrp",
        "browser_preview_compatible": False,
        "description": "Lossless RGB archive/diagnostic output with FLAC audio; very large files.",
    },
    CUSTOM_SAVE_VIDEO_PRESET: {
        "extension": ".mp4",
        "tensor_mode": DEFAULT_TENSOR_MODE,
        "codec": "",
        "pix_fmt": "",
        "browser_preview_compatible": False,
        "description": "Expert export controls for allowlisted video settings or PNG image sequences.",
    },
}

_FFMPEG_PATH: str | None = None
_FFPROBE_PATH: str | None = None


def _first_existing_path(candidates: Iterable[str]) -> str:
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return ""


def _find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path:
        return path

    try:
        import imageio_ffmpeg

        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except ImportError:
        logger.info("imageio-ffmpeg not found, attempting to install...")
        try:
            import sys

            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "imageio-ffmpeg"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            import imageio_ffmpeg

            path = imageio_ffmpeg.get_ffmpeg_exe()
            if path and os.path.isfile(path):
                return path
        except Exception as exc:
            logger.warning("Failed to install imageio-ffmpeg: %s", exc)
    except Exception:
        pass

    import sys

    python_dir = os.path.dirname(sys.executable)
    candidates = [
        os.path.join(python_dir, "ffmpeg.exe"),
        os.path.join(python_dir, "Scripts", "ffmpeg.exe"),
        os.path.join(python_dir, "..", "ffmpeg.exe"),
    ]
    try:
        import folder_paths

        comfy_base = folder_paths.base_path
        sm_data = os.path.dirname(os.path.dirname(comfy_base))
        candidates.extend([
            os.path.join(comfy_base, "ffmpeg.exe"),
            os.path.join(comfy_base, "ffmpeg", "ffmpeg.exe"),
            os.path.join(sm_data, "Assets", "ffmpeg", "ffmpeg.exe"),
            os.path.join(sm_data, "Assets", "ffmpeg", "bin", "ffmpeg.exe"),
        ])
    except Exception:
        pass

    site_binaries = os.path.join(python_dir, "Lib", "site-packages", "imageio_ffmpeg", "binaries")
    if os.path.isdir(site_binaries):
        for filename in os.listdir(site_binaries):
            lowered = filename.lower()
            if "ffmpeg" in lowered and "ffprobe" not in lowered:
                candidates.append(os.path.join(site_binaries, filename))
    candidates.extend([
        r"C:\ffmpeg\bin\ffmpeg.exe",
        os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe"),
    ])
    return _first_existing_path(candidates) or "ffmpeg"


def get_ffmpeg_path() -> str:
    global _FFMPEG_PATH
    if _FFMPEG_PATH is None:
        _FFMPEG_PATH = _find_ffmpeg()
    return _FFMPEG_PATH


def _find_ffprobe() -> str:
    path = shutil.which("ffprobe")
    if path:
        return path
    ffmpeg = get_ffmpeg_path()
    candidates = []
    if ffmpeg and ffmpeg != "ffmpeg":
        suffix = ".exe" if os.name == "nt" else ""
        candidates.append(os.path.join(os.path.dirname(ffmpeg), f"ffprobe{suffix}"))
    return _first_existing_path(candidates) or "ffprobe"


def get_ffprobe_path() -> str:
    global _FFPROBE_PATH
    if _FFPROBE_PATH is None:
        _FFPROBE_PATH = _find_ffprobe()
    return _FFPROBE_PATH


_TIMECODE_RE = re.compile(r"(?P<h>\d+):(?P<m>\d{2}):(?P<s>\d{2}(?:\.\d+)?)")
_DURATION_RE = re.compile(r"Duration:\s*(?P<value>N/A|\d+:\d{2}:\d{2}(?:\.\d+)?)")
_PROGRESS_TIME_RE = re.compile(r"time=(?P<value>\d+:\d{2}:\d{2}(?:\.\d+)?)")
_FRAME_RE = re.compile(r"frame=\s*(?P<value>\d+)")
_VIDEO_SIZE_RE = re.compile(r"(?P<w>\d{2,6})x(?P<h>\d{2,6})(?:\s|,|\[)")
_FPS_RE = re.compile(r"(?P<fps>\d+(?:\.\d+)?)\s*fps\b")


def _finite_positive(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(number) and number > 0


def _finite_positive_int(value) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


def _finite_positive_float(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _parse_fraction(value) -> float:
    text = str(value or "").strip()
    if not text or text == "0/0":
        return 0.0
    if "/" in text:
        try:
            left, right = text.split("/", 1)
            numerator = float(left)
            denominator = float(right)
            return numerator / denominator if denominator else 0.0
        except (TypeError, ValueError, ZeroDivisionError):
            return 0.0
    return _finite_positive_float(text)


def _timecode_seconds(value: str) -> float:
    match = _TIMECODE_RE.search(str(value or ""))
    if not match:
        return 0.0
    hours = int(match.group("h"))
    minutes = int(match.group("m"))
    seconds = float(match.group("s"))
    return hours * 3600.0 + minutes * 60.0 + seconds


def _duration_from_ffmpeg_text(text: str) -> float:
    match = _DURATION_RE.search(str(text or ""))
    if match and match.group("value") != "N/A":
        duration = _timecode_seconds(match.group("value"))
        if duration > 0:
            return duration
    progress = _PROGRESS_TIME_RE.findall(str(text or ""))
    if progress:
        duration = _timecode_seconds(progress[-1])
        if duration > 0:
            return duration
    return 0.0


def _run_text_command(cmd: list[str], *, timeout: int | float = 30) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception as exc:
        logger.debug("media probe command unavailable (%s): %s", cmd[0] if cmd else "", exc)
        return None


def _ffprobe_json(path: str, entries: str, *, select_streams: str = "") -> dict:
    cmd = [get_ffprobe_path(), "-v", "error"]
    if select_streams:
        cmd += ["-select_streams", select_streams]
    cmd += ["-show_entries", entries, "-of", "json", str(path)]
    result = _run_text_command(cmd, timeout=30)
    if not result or result.returncode != 0 or not (result.stdout or "").strip():
        return {}
    try:
        return json.loads(result.stdout or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _ffmpeg_input_text(path: str) -> str:
    result = _run_text_command([get_ffmpeg_path(), "-hide_banner", "-i", str(path)], timeout=30)
    if not result:
        return ""
    return f"{result.stdout or ''}\n{result.stderr or ''}"


def _ffmpeg_decode_null_text(path: str, *, stream: str = "", timeout: int | float = 120) -> tuple[int, str]:
    cmd = [get_ffmpeg_path(), "-hide_banner", "-i", str(path)]
    if stream:
        cmd += ["-map", stream]
    cmd += ["-f", "null", "-"]
    result = _run_text_command(cmd, timeout=timeout)
    if not result:
        return 1, ""
    return int(result.returncode or 0), f"{result.stdout or ''}\n{result.stderr or ''}"


def _mutagen_audio_metadata(path: str) -> tuple[float, int]:
    try:
        from mutagen import File as MutagenFile

        mf = MutagenFile(path)
        info = getattr(mf, "info", None) if mf is not None else None
        duration = _finite_positive_float(getattr(info, "length", 0.0))
        sample_rate = _finite_positive_int(getattr(info, "sample_rate", 0))
        if duration > 0:
            return duration, sample_rate
    except Exception as exc:
        logger.debug("mutagen audio probe failed for %s: %s", path, exc)
    return 0.0, 0


def probe_audio_metadata(path: str) -> dict:
    duration, sample_rate = _mutagen_audio_metadata(path)
    if duration > 0:
        return {"duration_sec": duration, "sample_rate": sample_rate}

    data = _ffprobe_json(
        path,
        "stream=sample_rate,duration:format=duration",
        select_streams="a:0",
    )
    stream = next(iter(data.get("streams", []) or []), {})
    fmt = data.get("format", {}) or {}
    duration = _finite_positive_float(stream.get("duration")) or _finite_positive_float(fmt.get("duration"))
    sample_rate = _finite_positive_int(stream.get("sample_rate"))
    if duration > 0:
        return {"duration_sec": duration, "sample_rate": sample_rate}

    text = _ffmpeg_input_text(path)
    duration = _duration_from_ffmpeg_text(text)
    if duration <= 0:
        _returncode, text = _ffmpeg_decode_null_text(path, stream="0:a:0", timeout=120)
        duration = _duration_from_ffmpeg_text(text)
    if duration <= 0:
        raise MediaProbeError(f"Could not probe usable audio duration for {os.path.basename(path)}")
    return {"duration_sec": duration, "sample_rate": sample_rate}


def probe_audio_duration(path: str) -> float:
    return float(probe_audio_metadata(path).get("duration_sec", 0.0) or 0.0)


def _opencv_video_metadata(path: str) -> dict:
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            return {}
        width = _finite_positive_int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = _finite_positive_int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        frame_count = _finite_positive_int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        fps = _finite_positive_float(cap.get(cv2.CAP_PROP_FPS))
        duration = (frame_count / fps) if frame_count > 0 and fps > 0 else 0.0
        if width > 0 and height > 0 and frame_count > 0 and fps > 0 and duration > 0:
            return {
                "width": width,
                "height": height,
                "frame_count": frame_count,
                "fps": fps,
                "duration_sec": duration,
            }
    finally:
        cap.release()
    return {}


def _ffprobe_video_metadata(path: str) -> dict:
    data = _ffprobe_json(
        path,
        "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration:format=duration",
        select_streams="v:0",
    )
    stream = next(iter(data.get("streams", []) or []), {})
    fmt = data.get("format", {}) or {}
    width = _finite_positive_int(stream.get("width"))
    height = _finite_positive_int(stream.get("height"))
    fps = _parse_fraction(stream.get("avg_frame_rate")) or _parse_fraction(stream.get("r_frame_rate"))
    duration = _finite_positive_float(stream.get("duration")) or _finite_positive_float(fmt.get("duration"))
    frame_count = _finite_positive_int(stream.get("nb_frames"))
    if frame_count <= 0 and duration > 0 and fps > 0:
        frame_count = max(1, int(round(duration * fps)))
    if duration <= 0 and frame_count > 0 and fps > 0:
        duration = frame_count / fps
    if width > 0 and height > 0 and frame_count > 0 and fps > 0 and duration > 0:
        return {
            "width": width,
            "height": height,
            "frame_count": frame_count,
            "fps": fps,
            "duration_sec": duration,
        }
    return {}


def _ffmpeg_video_metadata(path: str) -> dict:
    returncode, text = _ffmpeg_decode_null_text(path, stream="0:v:0", timeout=180)
    if returncode != 0 and not text:
        return {}

    video_line = next((line for line in text.splitlines() if " Video:" in line or line.strip().startswith("Stream") and "Video:" in line), "")
    size_match = _VIDEO_SIZE_RE.search(video_line)
    fps_match = _FPS_RE.search(video_line)
    width = _finite_positive_int(size_match.group("w")) if size_match else 0
    height = _finite_positive_int(size_match.group("h")) if size_match else 0
    fps = _finite_positive_float(fps_match.group("fps")) if fps_match else 0.0
    frame_matches = _FRAME_RE.findall(text)
    frame_count = _finite_positive_int(frame_matches[-1]) if frame_matches else 0
    duration = _duration_from_ffmpeg_text(text)
    if frame_count <= 0 and duration > 0 and fps > 0:
        frame_count = max(1, int(round(duration * fps)))
    if duration <= 0 and frame_count > 0 and fps > 0:
        duration = frame_count / fps
    if fps <= 0 and frame_count > 0 and duration > 0:
        fps = frame_count / duration
    if width > 0 and height > 0 and frame_count > 0 and fps > 0 and duration > 0:
        return {
            "width": width,
            "height": height,
            "frame_count": frame_count,
            "fps": fps,
            "duration_sec": duration,
        }
    return {}


def probe_media_has_audio(path: str) -> bool:
    data = _ffprobe_json(path, "stream=codec_type", select_streams="a")
    if any((stream.get("codec_type") == "audio") for stream in data.get("streams", []) or []):
        return True

    result = _run_text_command(
        [get_ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-map", "0:a:0", "-t", "0.1", "-f", "null", "-"],
        timeout=30,
    )
    return bool(result and result.returncode == 0)


def probe_video_metadata(path: str) -> dict:
    metadata = _ffprobe_video_metadata(path) or _opencv_video_metadata(path) or _ffmpeg_video_metadata(path)
    if not metadata:
        raise MediaProbeError(f"Could not probe usable video metadata for {os.path.basename(path)}")
    metadata["has_audio"] = probe_media_has_audio(path)
    return metadata


def probe_image_metadata(path: str) -> dict:
    try:
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
        width = _finite_positive_int(width)
        height = _finite_positive_int(height)
        if width > 0 and height > 0:
            return {"width": width, "height": height}
    except Exception as exc:
        logger.debug("image metadata probe failed for %s: %s", path, exc)
    raise MediaProbeError(f"Could not probe usable image metadata for {os.path.basename(path)}")


def empty_media_metadata() -> dict:
    return {
        "width": 0,
        "height": 0,
        "frame_count": 0,
        "fps": 0.0,
        "duration_sec": 0.0,
        "sample_rate": 0,
        "has_audio": False,
    }


def is_valid_media_metadata(metadata: dict, asset_type: str) -> bool:
    if asset_type == "video":
        return (
            _finite_positive(metadata.get("width"))
            and _finite_positive(metadata.get("height"))
            and _finite_positive(metadata.get("frame_count"))
            and _finite_positive(metadata.get("fps"))
            and _finite_positive(metadata.get("duration_sec"))
        )
    if asset_type == "image":
        return _finite_positive(metadata.get("width")) and _finite_positive(metadata.get("height"))
    if asset_type == "audio":
        return _finite_positive(metadata.get("duration_sec"))
    return True


def validate_media_metadata(metadata: dict, asset_type: str, path: str = "") -> None:
    if not is_valid_media_metadata(metadata, asset_type):
        label = os.path.basename(path) if path else "media"
        raise MediaProbeError(f"Could not probe usable {asset_type} metadata for {label}")


def probe_media_metadata(path: str, asset_type: str, *, strict: bool = False) -> dict:
    metadata = empty_media_metadata()
    try:
        if asset_type == "video":
            metadata.update(probe_video_metadata(path))
        elif asset_type == "image":
            metadata.update(probe_image_metadata(path))
        elif asset_type == "audio":
            metadata.update(probe_audio_metadata(path))
    except MediaProbeError:
        if strict:
            raise
    if strict:
        validate_media_metadata(metadata, asset_type, path)
    return metadata


def decode_audio_samples(
    path: str,
    *,
    sample_rate: int = 44100,
    channels: int = 1,
    mix_to_mono: bool = True,
    timeout: int | float = 60,
) -> tuple[np.ndarray, int]:
    sample_rate = max(1, int(sample_rate or 44100))
    channels = max(1, int(channels or 1))
    try:
        result = subprocess.run(
            [
                get_ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-f",
                "s16le",
                "-ac",
                str(channels),
                "-ar",
                str(sample_rate),
                "pipe:1",
            ],
            capture_output=True,
            timeout=timeout,
        )
    except Exception as exc:
        raise MediaProbeError(f"Could not decode audio samples for {os.path.basename(path)}: {exc}") from exc
    if result.returncode != 0 or not result.stdout:
        stderr = (result.stderr or b"").decode(errors="replace").strip()
        raise MediaProbeError(f"Could not decode audio samples for {os.path.basename(path)}: {stderr[:240]}")
    samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels)
        if mix_to_mono:
            samples = samples.mean(axis=1)
        else:
            samples = samples.T.copy()
    return samples, sample_rate


def write_audio_wav(path: str, samples: np.ndarray, sample_rate: int) -> None:
    """Write float audio samples to a 16-bit PCM WAV file.

    Accepts mono samples shaped (N,) or channel-first samples shaped (channels, N).
    """
    sample_rate = max(1, int(sample_rate or 44100))
    arr = np.asarray(samples, dtype=np.float32)
    if arr.ndim == 0:
        arr = arr.reshape(1)
    if arr.ndim == 1:
        channels = 1
        interleaved = arr
    elif arr.ndim == 2:
        channels = int(arr.shape[0])
        if channels < 1:
            raise ValueError("Audio waveform must have at least one channel")
        interleaved = arr.T.reshape(-1)
    else:
        raise ValueError(f"Unsupported audio waveform shape for WAV export: {arr.shape}")

    pcm = (np.clip(interleaved, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(channels)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(pcm.tobytes())


def _parse_metadata_json(value):
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _workflow_from_metadata_tags(tags: dict):
    if not isinstance(tags, dict):
        return None
    editor_export = None
    for key, value in tags.items():
        normalized = str(key or "").lower()
        if normalized == "workflow":
            parsed = _parse_metadata_json(value)
            if isinstance(parsed, dict):
                return parsed
        elif normalized == "editor_export":
            parsed = _parse_metadata_json(value)
            if isinstance(parsed, dict):
                editor_export = parsed
    workflow = editor_export.get("workflow") if isinstance(editor_export, dict) else None
    return workflow if isinstance(workflow, dict) else None


def _ffmetadata_unescape(value: str) -> str:
    result = []
    escaped = False
    replacements = {"n": "\n", "r": "\r"}
    for char in str(value):
        if escaped:
            result.append(replacements.get(char, char))
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            result.append(char)
    if escaped:
        result.append("\\")
    return "".join(result)


def _split_ffmetadata_line(line: str) -> tuple[str, str] | None:
    escaped = False
    for index, char in enumerate(str(line)):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "=":
            return (_ffmetadata_unescape(line[:index]), _ffmetadata_unescape(line[index + 1:]))
    return None


def _parse_ffmetadata_tags(text: str) -> dict:
    tags = {}
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line == ";FFMETADATA1" or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            continue
        pair = _split_ffmetadata_line(raw_line)
        if not pair:
            continue
        key, value = pair
        if key:
            tags[key] = value
    return tags


def extract_embedded_workflow_metadata(path: str):
    ext = os.path.splitext(str(path or ""))[1].lower()
    if ext == ".png":
        try:
            from PIL import Image

            with Image.open(path) as image:
                return _workflow_from_metadata_tags(image.info or {})
        except Exception as exc:
            logger.debug("PNG workflow metadata extraction failed for %s: %s", path, exc)
            return None
    if ext not in {".mp4", ".m4v", ".mov", ".mkv"}:
        return None

    try:
        result = subprocess.run(
            [
                get_ffprobe_path(),
                "-v",
                "quiet",
                "-show_format",
                "-print_format",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")
            workflow = _workflow_from_metadata_tags(((data.get("format") or {}).get("tags") or {}))
            if workflow is not None:
                return workflow
        else:
            logger.debug("ffprobe workflow metadata extraction failed for %s: %s", path, (result.stderr or "")[:240])
    except Exception as exc:
        logger.debug("ffprobe workflow metadata extraction failed for %s: %s", path, exc)

    try:
        result = subprocess.run(
            [
                get_ffmpeg_path(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-f",
                "ffmetadata",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug("ffmpeg workflow metadata extraction failed for %s: %s", path, (result.stderr or "")[:240])
            return None
        return _workflow_from_metadata_tags(_parse_ffmetadata_tags(result.stdout or ""))
    except Exception as exc:
        logger.debug("ffmpeg workflow metadata extraction failed for %s: %s", path, exc)
        return None


def normalize_save_preset(preset_id: str | None) -> str:
    candidate = str(preset_id or "").strip()
    return candidate if candidate in SAVE_VIDEO_PRESETS else DEFAULT_SAVE_VIDEO_PRESET


def output_extension_for_preset(preset_id: str | None) -> str:
    preset = SAVE_VIDEO_PRESETS[normalize_save_preset(preset_id)]
    return str(preset["extension"])


def tensor_mode_for_preset(preset_id: str | None) -> str:
    preset = SAVE_VIDEO_PRESETS[normalize_save_preset(preset_id)]
    return str(preset["tensor_mode"])


def _pick_allowed(value, allowed: list[str], default: str) -> str:
    candidate = str(value or "").strip()
    return candidate if candidate in allowed else default


def _clamp_int(value, min_value: int, max_value: int, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed))


def resolve_custom_export_options(options: dict | None = None) -> dict:
    source = options if isinstance(options, dict) else {}
    output_kind = _pick_allowed(
        source.get("custom_output_kind"),
        CUSTOM_OUTPUT_KIND_OPTIONS,
        CUSTOM_OUTPUT_KIND_VIDEO,
    )
    container = _pick_allowed(source.get("custom_container"), CUSTOM_CONTAINER_OPTIONS, "mp4")
    video_codec = _pick_allowed(source.get("custom_video_codec"), CUSTOM_VIDEO_CODEC_OPTIONS, "libx264")
    pix_fmt = _pick_allowed(source.get("custom_pix_fmt"), CUSTOM_PIX_FMT_OPTIONS, "yuv420p")
    encoder_preset = _pick_allowed(source.get("custom_encoder_preset"), CUSTOM_ENCODER_PRESET_OPTIONS, "slow")
    audio_codec = _pick_allowed(source.get("custom_audio_codec"), CUSTOM_AUDIO_CODEC_OPTIONS, "aac")
    tensor_mode = DEFAULT_TENSOR_MODE
    crf = _clamp_int(source.get("custom_crf"), 0, 51, 18)
    audio_bitrate_kbps = _clamp_int(source.get("custom_audio_bitrate_kbps"), 1, 10000, 192)
    png_compression = _clamp_int(source.get("custom_png_compression"), 0, 9, 0)
    return {
        "output_kind": output_kind,
        "container": container,
        "extension": ".png" if output_kind == CUSTOM_OUTPUT_KIND_PNG_SEQUENCE else f".{container}",
        "video_codec": video_codec,
        "pix_fmt": pix_fmt,
        "crf": crf,
        "encoder_preset": encoder_preset,
        "audio_codec": audio_codec,
        "audio_bitrate_kbps": audio_bitrate_kbps,
        "tensor_mode": tensor_mode,
        "png_compression": png_compression,
        "browser_preview_compatible": (
            output_kind == CUSTOM_OUTPUT_KIND_VIDEO
            and container == "mp4"
            and video_codec == "libx264"
            and pix_fmt == "yuv420p"
        ),
    }


def output_extension_for_custom_options(options: dict | None = None) -> str:
    return str(resolve_custom_export_options(options)["extension"])


def save_video_encode_timeout_seconds(
    preset_id: str | None,
    frame_count: int,
    width: int,
    height: int,
    custom_options: dict | None = None,
) -> int:
    preset_id = normalize_save_preset(preset_id)
    frame_count = max(1, int(frame_count or 1))
    width = max(1, int(width or 1))
    height = max(1, int(height or 1))
    megapixel_frames = (frame_count * width * height) / 1_000_000

    if preset_id == CUSTOM_SAVE_VIDEO_PRESET:
        spec = resolve_custom_export_options(custom_options)
        if spec["output_kind"] != CUSTOM_OUTPUT_KIND_VIDEO:
            return MIN_SAVE_VIDEO_ENCODE_TIMEOUT_SECONDS
        seconds_per_mpf = _CUSTOM_CODEC_TIMEOUT_BASE.get(str(spec["video_codec"]), 0.6)
        seconds_per_mpf *= _CUSTOM_ENCODER_PRESET_TIMEOUT_MULTIPLIER.get(str(spec["encoder_preset"]), 1.0)
        seconds_per_mpf *= _PIX_FMT_TIMEOUT_MULTIPLIER.get(str(spec["pix_fmt"]), 1.0)
    else:
        seconds_per_mpf = _PRESET_TIMEOUT_SECONDS_PER_MEGAPIXEL_FRAME.get(
            preset_id,
            _PRESET_TIMEOUT_SECONDS_PER_MEGAPIXEL_FRAME[DEFAULT_SAVE_VIDEO_PRESET],
        )

    estimate = int(math.ceil(megapixel_frames * seconds_per_mpf))
    return max(
        MIN_SAVE_VIDEO_ENCODE_TIMEOUT_SECONDS,
        min(MAX_SAVE_VIDEO_ENCODE_TIMEOUT_SECONDS, estimate),
    )


def tensor_to_uint8_frames(tensor, *, mode: str = "truncate") -> np.ndarray:
    arr = tensor.detach().cpu().numpy() if hasattr(tensor, "detach") else np.asarray(tensor)
    scaled = np.asarray(arr, dtype=np.float32) * 255.0
    if mode == "round":
        scaled = np.rint(scaled)
    elif mode != "truncate":
        raise ValueError(f"Unknown tensor conversion mode: {mode}")
    return scaled.clip(0, 255).astype(np.uint8)


# Per-item fit-mode vocabulary (shared with timeline_state defaults / routes validation).
# The default values below ARE the fixed code constants legacy projects deserialize to.
FIT_MODES = ("fit", "pad_edge", "cover", "stretch")
CROP_POSITIONS = ("center", "top", "bottom", "left", "right")
DEFAULT_FIT_MODE = "pad_edge"
DEFAULT_CROP_POSITION = "center"


def _resize_interpolation(src_w: int, src_h: int, dst_w: int, dst_h: int) -> int:
    """Auto interpolation: downscale → INTER_AREA (anti-aliased), upscale → INTER_LANCZOS4 (sharp)."""
    return cv2.INTER_AREA if (dst_w * dst_h) < (src_w * src_h) else cv2.INTER_LANCZOS4


def fit_frame_to_canvas(
    frame_rgb: np.ndarray,
    canvas_w: int,
    canvas_h: int,
    mode: str = DEFAULT_FIT_MODE,
    crop_position: str = DEFAULT_CROP_POSITION,
) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Fit a source frame onto a scene-sized canvas.

    Returns (canvas, (x_off, y_off, w, h)) where the bounds are the region to composite:
    `fit` returns the inner content rect so black bars stay transparent to lower layers;
    `pad_edge`/`cover`/`stretch` return the full canvas (the whole frame is content).
    """
    fh, fw = frame_rgb.shape[:2]
    canvas_w = max(1, int(canvas_w))
    canvas_h = max(1, int(canvas_h))
    if fw <= 0 or fh <= 0:
        return np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8), (0, 0, 0, 0)

    mode = mode if mode in FIT_MODES else DEFAULT_FIT_MODE
    crop_position = crop_position if crop_position in CROP_POSITIONS else DEFAULT_CROP_POSITION

    if mode == "stretch":
        interp = _resize_interpolation(fw, fh, canvas_w, canvas_h)
        canvas = cv2.resize(frame_rgb, (canvas_w, canvas_h), interpolation=interp)
        return canvas, (0, 0, canvas_w, canvas_h)

    if mode == "cover":
        scale = max(canvas_w / fw, canvas_h / fh)
        new_w = max(canvas_w, int(round(fw * scale)))
        new_h = max(canvas_h, int(round(fh * scale)))
        interp = _resize_interpolation(fw, fh, new_w, new_h)
        resized = cv2.resize(frame_rgb, (new_w, new_h), interpolation=interp)
        x_extra = new_w - canvas_w
        y_extra = new_h - canvas_h
        if crop_position == "left":
            x_crop = 0
        elif crop_position == "right":
            x_crop = x_extra
        else:
            x_crop = x_extra // 2
        if crop_position == "top":
            y_crop = 0
        elif crop_position == "bottom":
            y_crop = y_extra
        else:
            y_crop = y_extra // 2
        x_crop = max(0, min(x_crop, x_extra))
        y_crop = max(0, min(y_crop, y_extra))
        canvas = np.ascontiguousarray(resized[y_crop:y_crop + canvas_h, x_crop:x_crop + canvas_w])
        return canvas, (0, 0, canvas_w, canvas_h)

    # fit / pad_edge: contain, centered
    scale = min(canvas_w / fw, canvas_h / fh)
    new_w = max(1, int(fw * scale))
    new_h = max(1, int(fh * scale))
    interp = _resize_interpolation(fw, fh, new_w, new_h)
    resized = cv2.resize(frame_rgb, (new_w, new_h), interpolation=interp)
    x_off = (canvas_w - new_w) // 2
    y_off = (canvas_h - new_h) // 2

    if mode == "pad_edge":
        top = y_off
        bottom = canvas_h - new_h - top
        left = x_off
        right = canvas_w - new_w - left
        canvas = cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_REPLICATE)
        return canvas, (0, 0, canvas_w, canvas_h)

    # mode == "fit": black bars; return inner content rect so bars are NOT composited
    canvas = np.zeros((canvas_h, canvas_w, 3), dtype=np.uint8)
    canvas[y_off:y_off + new_h, x_off:x_off + new_w] = resized
    return canvas, (x_off, y_off, new_w, new_h)


def resize_frame_to_long_edge(frame_rgb: np.ndarray, target_long_edge: int) -> np.ndarray:
    target = int(target_long_edge or 0)
    if target <= 0:
        return frame_rgb
    h, w = frame_rgb.shape[:2]
    source_long = max(w, h)
    if source_long <= 0 or source_long == target:
        return frame_rgb
    scale = target / source_long
    new_w = max(1, int(round(w * scale)))
    new_h = max(1, int(round(h * scale)))
    return cv2.resize(frame_rgb, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)


def probe_video_size(path: str) -> tuple[int, int]:
    ffprobe = get_ffprobe_path()
    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")
            stream = next(iter(data.get("streams", []) or []), {})
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            if width > 0 and height > 0:
                return width, height
        logger.debug("ffprobe size probe failed for %s: %s", path, (result.stderr or "").strip()[:240])
    except Exception as exc:
        logger.debug("ffprobe size probe unavailable for %s: %s", path, exc)

    cap = cv2.VideoCapture(str(path))
    try:
        if cap.isOpened():
            width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            if width > 0 and height > 0:
                return width, height
    finally:
        cap.release()
    raise RuntimeError(f"Could not probe video size for {path}")


def decode_video_range(
    path: str,
    start: int,
    end_exclusive: int,
    *,
    target_w: int | None = None,
    target_h: int | None = None,
) -> Iterator[np.ndarray]:
    start_frame = max(0, int(start or 0))
    end_frame = max(start_frame, int(end_exclusive or 0))
    frame_count = end_frame - start_frame
    if frame_count <= 0:
        return

    if target_w and target_h:
        width = max(1, int(target_w))
        height = max(1, int(target_h))
    else:
        width, height = probe_video_size(path)

    filters = [f"select=between(n\\,{start_frame}\\,{end_frame - 1})"]
    if target_w and target_h:
        filters.append(f"scale={width}:{height}")
    cmd = [
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-vf",
        ",".join(filters),
        "-vsync",
        "0",
        "-frames:v",
        str(frame_count),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "pipe:1",
    ]
    timeout = max(30, min(600, frame_count * 5))
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[:500])

    frame_size = width * height * 3
    decoded_count = len(result.stdout) // frame_size
    for idx in range(min(decoded_count, frame_count)):
        offset = idx * frame_size
        frame = np.frombuffer(result.stdout[offset:offset + frame_size], dtype=np.uint8)
        yield frame.reshape((height, width, 3)).copy()


def decode_video_frame(path: str, frame_index: int) -> np.ndarray | None:
    try:
        return next(decode_video_range(path, frame_index, int(frame_index) + 1), None)
    except Exception as exc:
        logger.warning("ffmpeg frame decode failed for %s frame %s: %s", path, frame_index, exc)
        return None


def write_png(path: str, frame_rgb: np.ndarray, *, compression: int = 0, metadata: dict[str, str] | None = None) -> None:
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    os.makedirs(os.path.dirname(path), exist_ok=True)
    pnginfo = None
    if metadata:
        pnginfo = PngInfo()
        for key, value in metadata.items():
            if not key:
                continue
            # ComfyUI's canvas drag workflow reader is most compatible with
            # plain text PNG chunks. Keep these uncompressed so generated PNGs
            # round-trip through the native workflow drop path.
            pnginfo.add_text(str(key), str(value), zip=False)
    Image.fromarray(np.asarray(frame_rgb, dtype=np.uint8), mode="RGB").save(
        path,
        compress_level=_clamp_int(compression, 0, 9, 0),
        pnginfo=pnginfo,
    )


_FRAME_SHAPE_ERROR = "frames_iter must provide one or more RGB frames shaped (H, W, 3)"


def _close_iterable(value) -> None:
    close = getattr(value, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _normalize_rgb_frame(frame, *, expected_shape: tuple[int, int, int] | None = None) -> np.ndarray:
    arr = np.asarray(frame)
    if arr.ndim != 3 or arr.shape[-1] != 3 or arr.shape[0] <= 0 or arr.shape[1] <= 0:
        raise ValueError(_FRAME_SHAPE_ERROR)
    if expected_shape is not None and tuple(arr.shape) != tuple(expected_shape):
        raise ValueError("frames_iter must provide same-sized RGB frames")
    if arr.dtype != np.uint8:
        arr = np.asarray(arr).clip(0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


def _non_array_frame_stream(frames_iter: Iterable[np.ndarray]) -> tuple[Iterator[np.ndarray], int, int]:
    source = frames_iter
    iterator = iter(frames_iter)

    def close_source() -> None:
        _close_iterable(iterator)
        if iterator is not source:
            _close_iterable(source)

    try:
        first = _normalize_rgb_frame(next(iterator))
    except StopIteration:
        close_source()
        raise ValueError(_FRAME_SHAPE_ERROR) from None
    except Exception:
        close_source()
        raise

    expected_shape = tuple(first.shape)

    def stream() -> Iterator[np.ndarray]:
        try:
            yield first
            for frame in iterator:
                yield _normalize_rgb_frame(frame, expected_shape=expected_shape)
        finally:
            close_source()

    return stream(), int(first.shape[0]), int(first.shape[1])


def _prepare_frame_stream(frames_iter: Iterable[np.ndarray]) -> tuple[Iterable[np.ndarray], int | None, int, int]:
    if isinstance(frames_iter, np.ndarray):
        frames = _coerce_frames_array(frames_iter)
        frame_count, h, w = frames.shape[:3]
        return frames, int(frame_count), int(h), int(w)
    frames, h, w = _non_array_frame_stream(frames_iter)
    return frames, None, h, w


def _coerce_frames_array(frames_iter: Iterable[np.ndarray]) -> np.ndarray:
    if isinstance(frames_iter, np.ndarray):
        frames = frames_iter
    else:
        stream, _, _ = _non_array_frame_stream(frames_iter)
        frames = np.stack(list(stream), axis=0)
    if frames.ndim != 4 or frames.shape[-1] != 3 or frames.shape[0] <= 0 or frames.shape[1] <= 0 or frames.shape[2] <= 0:
        raise ValueError(_FRAME_SHAPE_ERROR)
    if frames.dtype != np.uint8:
        frames = np.asarray(frames).clip(0, 255).astype(np.uint8)
    return np.ascontiguousarray(frames)


def _read_process_stderr(stderr_file) -> bytes:
    try:
        stderr_file.flush()
        stderr_file.seek(0)
        return stderr_file.read()
    except Exception:
        return b""


_FFMPEG_ERROR_MARKERS = (
    "error", "invalid", "could not", "cannot", "unable", "failed",
    "no such", "not found", "denied", "unsupported", "conversion failed",
    "broken pipe", "permission",
)


def _ffmpeg_failed_message(stderr: bytes) -> str:
    text = stderr.decode(errors="replace").strip()
    if not text:
        return "ffmpeg failed (no stderr captured)."
    # The old head slice only showed ffmpeg's version/configuration banner. A
    # blind tail is also unreliable: per-encoder summary statistics (coded %,
    # kb/s, Qavg) trail AFTER the fatal reason, so the real error gets pushed out
    # of the window. Prefer lines that carry an error marker; fall back to the
    # tail when none are found.
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    err_lines = [ln for ln in lines if any(m in ln.lower() for m in _FFMPEG_ERROR_MARKERS)]
    chosen = err_lines[-6:] if err_lines else lines[-6:]
    out = "\n".join(chosen)
    if len(out) > 600:
        out = out[-600:]
    return f"ffmpeg failed: {out}"


def _timeout_expired(cmd: list, timeout: int | float | None, stderr_file) -> subprocess.TimeoutExpired:
    return subprocess.TimeoutExpired(cmd, timeout, stderr=_read_process_stderr(stderr_file))


def _raise_stream_timeout(proc, cmd: list, timeout: int | float | None, stderr_file) -> None:
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass
    raise _timeout_expired(cmd, timeout, stderr_file)


def _cancel_requested(cancel_event) -> bool:
    return bool(cancel_event is not None and cancel_event.is_set())


def _terminate_process(proc) -> None:
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.wait(timeout=2)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def run_ffmpeg_command(
    cmd: list,
    *,
    timeout: int | float | None = None,
    cancel_event=None,
) -> None:
    deadline = time.perf_counter() + float(timeout) if timeout is not None else None
    with tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )
        while True:
            if _cancel_requested(cancel_event):
                _terminate_process(proc)
                raise MediaOperationCancelled("media operation cancelled")

            returncode = proc.poll()
            if returncode is not None:
                stderr = _read_process_stderr(stderr_file)
                if returncode != 0:
                    raise RuntimeError(_ffmpeg_failed_message(stderr))
                return

            if deadline is not None and time.perf_counter() >= deadline:
                _terminate_process(proc)
                raise _timeout_expired(cmd, timeout, stderr_file)

            time.sleep(0.1)


def _run_ffmpeg_streaming_frames(
    cmd: list,
    frames: Iterable[np.ndarray],
    *,
    timeout: int | float | None,
    cancel_event=None,
    progress_callback=None,
    frame_count: int | None = None,
) -> None:
    deadline = time.perf_counter() + float(timeout) if timeout is not None else None
    with tempfile.TemporaryFile() as stderr_file:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
        )
        timed_out = threading.Event()

        def kill_on_timeout() -> None:
            timed_out.set()
            try:
                proc.kill()
            except Exception:
                pass

        timer = threading.Timer(float(timeout), kill_on_timeout) if timeout is not None else None
        if timer is not None:
            timer.daemon = True
            timer.start()

        broken_pipe = False
        try:
            if proc.stdin is None:
                raise RuntimeError("ffmpeg failed: stdin pipe was not available")

            written = 0
            terminated = False
            try:
                for frame in frames:
                    if _cancel_requested(cancel_event):
                        _terminate_process(proc)
                        terminated = True
                        raise MediaOperationCancelled("media operation cancelled")
                    if timed_out.is_set():
                        _raise_stream_timeout(proc, cmd, timeout, stderr_file)
                    try:
                        proc.stdin.write(memoryview(frame).cast("B"))
                    except (BrokenPipeError, OSError):
                        broken_pipe = True
                        break
                    written += 1
                    if progress_callback is not None:
                        try:
                            progress_callback(written)
                        except Exception:
                            pass
            except BaseException:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                if not terminated and not timed_out.is_set():
                    _terminate_process(proc)
                raise
            finally:
                _close_iterable(frames)

            try:
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                broken_pipe = True

            if timed_out.is_set():
                _raise_stream_timeout(proc, cmd, timeout, stderr_file)

            if _cancel_requested(cancel_event):
                _terminate_process(proc)
                raise MediaOperationCancelled("media operation cancelled")

            if cancel_event is None:
                wait_timeout = None
                if deadline is not None:
                    wait_timeout = max(0.0, deadline - time.perf_counter())
                try:
                    returncode = proc.wait(timeout=wait_timeout)
                except subprocess.TimeoutExpired:
                    _raise_stream_timeout(proc, cmd, timeout, stderr_file)
            else:
                while True:
                    if _cancel_requested(cancel_event):
                        _terminate_process(proc)
                        raise MediaOperationCancelled("media operation cancelled")
                    if hasattr(proc, "poll"):
                        returncode = proc.poll()
                        if returncode is not None:
                            break
                    else:
                        try:
                            returncode = proc.wait(timeout=0.1)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                    if deadline is not None and time.perf_counter() >= deadline:
                        _raise_stream_timeout(proc, cmd, timeout, stderr_file)
                    time.sleep(0.1)
            if timer is not None:
                timer.cancel()

            if timed_out.is_set():
                _raise_stream_timeout(proc, cmd, timeout, stderr_file)

            stderr = _read_process_stderr(stderr_file)
            if returncode != 0:
                raise RuntimeError(_ffmpeg_failed_message(stderr))
            if broken_pipe:
                # ffmpeg closed stdin before the writer loop finished, but exited
                # 0 — it produced a valid file. This is a benign shutdown race:
                # with `-shortest` ffmpeg stops at the (shorter) audio boundary,
                # and fast black/empty-frame encodes drain stdin and finish before
                # Python sends the last frames. Treating broken_pipe as a failure
                # here is what made empty-space/short exports fail with a bogus
                # "ffmpeg failed: <encoder summary stats>" message. Trust the exit
                # code; only log the early close for observability.
                logger.warning(
                    "ffmpeg closed input early (wrote %d/%s frames) but exited 0; treating encode as success",
                    written,
                    frame_count if frame_count is not None else "unknown",
                )
        finally:
            if timer is not None:
                timer.cancel()


def _custom_video_args(spec: dict) -> list[str]:
    codec = str(spec["video_codec"])
    pix_fmt = str(spec["pix_fmt"])
    if codec in {"libx264", "libx265"}:
        args = [
            "-c:v", codec,
            "-preset", str(spec["encoder_preset"]),
            "-crf", str(int(spec["crf"])),
            "-pix_fmt", pix_fmt,
        ]
    elif codec == "prores_ks":
        args = [
            "-c:v", "prores_ks",
            "-profile:v", "3",
            "-pix_fmt", pix_fmt,
            "-vendor", "apl0",
            "-bits_per_mb", "8000",
        ]
    else:
        args = [
            "-c:v", "ffv1",
            "-level", "3",
            "-coder", "1",
            "-context", "1",
            "-g", "1",
            "-slices", "24",
            "-slicecrc", "1",
            "-pix_fmt", pix_fmt,
        ]
    if spec["container"] == "mp4":
        args += ["-movflags", "+faststart"]
    return args


def _custom_audio_args(spec: dict) -> list[str]:
    codec = str(spec["audio_codec"])
    if codec == "none":
        return []
    if codec == "aac":
        return ["-c:a", "aac", "-b:a", f"{int(spec['audio_bitrate_kbps'])}k"]
    return ["-c:a", codec]


def _audio_mode_from_args(args: list[str]) -> str:
    if not args:
        return "none"
    codec = ""
    bitrate = ""
    for idx, value in enumerate(args):
        if value == "-c:a" and idx + 1 < len(args):
            codec = str(args[idx + 1])
        elif value == "-b:a" and idx + 1 < len(args):
            bitrate = str(args[idx + 1])
    return f"{codec} {bitrate}".strip() or "none"


def _preset_video_args(preset_id: str, custom_options: dict | None = None) -> list[str]:
    if preset_id == CUSTOM_SAVE_VIDEO_PRESET:
        spec = resolve_custom_export_options(custom_options)
        if spec["output_kind"] != CUSTOM_OUTPUT_KIND_VIDEO:
            raise ValueError("Custom PNG Sequence is not a video encode preset")
        return _custom_video_args(spec)
    return list(SAVE_VIDEO_PRESETS[preset_id]["video_args"])


def _preset_audio_args(preset_id: str, custom_options: dict | None = None) -> list[str]:
    if preset_id == CUSTOM_SAVE_VIDEO_PRESET:
        spec = resolve_custom_export_options(custom_options)
        return _custom_audio_args(spec)
    return list(SAVE_VIDEO_PRESETS[preset_id]["audio_args"])


def _preset_metadata(preset_id: str, custom_options: dict | None = None) -> dict:
    preset = SAVE_VIDEO_PRESETS[preset_id]
    if preset_id == CUSTOM_SAVE_VIDEO_PRESET:
        spec = resolve_custom_export_options(custom_options)
        extension = str(spec["extension"])
        description = str(preset.get("description") or "")
        audio_mode = "none"
        if spec["output_kind"] == CUSTOM_OUTPUT_KIND_VIDEO:
            audio_mode = _audio_mode_from_args(_custom_audio_args(spec))
        metadata = {
            "label": CUSTOM_SAVE_VIDEO_PRESET,
            "description": description,
            "extension": extension,
            "save_preset": CUSTOM_SAVE_VIDEO_PRESET,
            "codec": "png" if spec["output_kind"] == CUSTOM_OUTPUT_KIND_PNG_SEQUENCE else str(spec["video_codec"]),
            "pix_fmt": "rgb24" if spec["output_kind"] == CUSTOM_OUTPUT_KIND_PNG_SEQUENCE else str(spec["pix_fmt"]),
            "container": "png" if spec["output_kind"] == CUSTOM_OUTPUT_KIND_PNG_SEQUENCE else str(spec["container"]),
            "tensor_mode": str(spec["tensor_mode"]),
            "audio_mode": audio_mode,
            "browser_preview_compatible": bool(spec["browser_preview_compatible"]),
            "custom_output_kind": str(spec["output_kind"]),
            "custom_container": str(spec["container"]),
            "custom_video_codec": str(spec["video_codec"]),
            "custom_pix_fmt": str(spec["pix_fmt"]),
            "custom_crf": int(spec["crf"]),
            "custom_encoder_preset": str(spec["encoder_preset"]),
            "custom_audio_codec": str(spec["audio_codec"]),
            "custom_audio_bitrate_kbps": int(spec["audio_bitrate_kbps"]),
            "custom_png_compression": int(spec["png_compression"]),
        }
        return metadata
    return {
        "label": preset_id,
        "description": str(preset.get("description") or ""),
        "extension": str(preset["extension"]),
        "save_preset": preset_id,
        "codec": str(preset.get("codec") or ""),
        "pix_fmt": str(preset.get("pix_fmt") or ""),
        "container": str(preset["extension"]).lstrip("."),
        "tensor_mode": str(preset["tensor_mode"]),
        "audio_mode": _audio_mode_from_args(list(preset.get("audio_args") or [])),
        "browser_preview_compatible": bool(preset.get("browser_preview_compatible", True)),
    }


def metadata_for_save_preset(preset_id: str | None, custom_options: dict | None = None) -> dict:
    return _preset_metadata(normalize_save_preset(preset_id), custom_options)


def _escape_ffmetadata(value: str) -> str:
    text = str(value)
    replacements = {
        "\\": "\\\\",
        "=": "\\=",
        ";": "\\;",
        "#": "\\#",
        "\n": "\\n",
        "\r": "\\r",
    }
    return "".join(replacements.get(char, char) for char in text)


def _write_ffmetadata_file(metadata: dict[str, str]) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        suffix=".ffmetadata",
    )
    try:
        handle.write(";FFMETADATA1\n")
        for key, value in metadata.items():
            if not key:
                continue
            handle.write(f"{_escape_ffmetadata(str(key))}={_escape_ffmetadata(str(value))}\n")
        return handle.name
    finally:
        handle.close()


def _video_args_with_metadata_movflags(video_args: list[str], output_path: str) -> list[str]:
    ext = os.path.splitext(str(output_path or ""))[1].lower()
    if ext not in {".mp4", ".m4v", ".mov"}:
        return video_args
    args = list(video_args)
    for index, value in enumerate(args):
        if value != "-movflags" or index + 1 >= len(args):
            continue
        flags = str(args[index + 1] or "")
        if "use_metadata_tags" not in flags:
            prefix = "+" if flags.startswith("+") else ""
            normalized = flags.lstrip("+")
            parts = [part for part in normalized.split("+") if part]
            parts.append("use_metadata_tags")
            args[index + 1] = prefix + "+".join(parts)
        return args
    args += ["-movflags", "+use_metadata_tags"]
    return args


def encode_video(
    frames_iter: Iterable[np.ndarray],
    *,
    preset_id: str,
    output_path: str,
    fps: float,
    audio_path: str | None = None,
    custom_options: dict | None = None,
    timeout: int = 90,
    cancel_event=None,
    embed_metadata: dict[str, str] | None = None,
    progress_callback=None,
) -> dict:
    preset_id = normalize_save_preset(preset_id)
    if preset_id == CUSTOM_SAVE_VIDEO_PRESET:
        custom_spec = resolve_custom_export_options(custom_options)
        if custom_spec["output_kind"] != CUSTOM_OUTPUT_KIND_VIDEO:
            raise ValueError("Custom PNG Sequence must be saved through the PNG sequence path")
    audio_args = _preset_audio_args(preset_id, custom_options)
    frames, frame_count, h, w = _prepare_frame_stream(frames_iter)
    fps_value = max(0.001, float(fps or 24.0))
    cmd = [
        get_ffmpeg_path(),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{w}x{h}",
        "-r",
        str(fps_value),
        "-i",
        "pipe:0",
    ]

    has_audio = bool(audio_path) and bool(audio_args)
    if has_audio:
        cmd += ["-i", str(audio_path)]

    ffmetadata_path = ""
    if embed_metadata:
        ffmetadata_path = _write_ffmetadata_file(embed_metadata)
        cmd += ["-i", ffmetadata_path]
        metadata_input_index = 2 if has_audio else 1
    else:
        metadata_input_index = -1

    video_args = _preset_video_args(preset_id, custom_options)
    if embed_metadata:
        video_args = _video_args_with_metadata_movflags(video_args, output_path)
    cmd += video_args
    if embed_metadata:
        cmd += ["-map_metadata", str(metadata_input_index)]
    if has_audio:
        cmd += ["-map", "0:v:0", "-map", "1:a:0", *audio_args, "-shortest"]
    elif embed_metadata:
        cmd += ["-map", "0:v:0"]
    cmd += [str(output_path), "-y"]

    started_at = time.perf_counter()
    try:
        _run_ffmpeg_streaming_frames(
            cmd,
            frames,
            timeout=timeout,
            cancel_event=cancel_event,
            progress_callback=progress_callback,
        )
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg timeout: encode_video output=%s", output_path)
        raise
    except MediaOperationCancelled:
        try:
            if os.path.isfile(output_path):
                os.remove(output_path)
        except OSError:
            pass
        raise
    finally:
        if ffmetadata_path:
            try:
                os.remove(ffmetadata_path)
            except OSError:
                pass
    frame_count_label = frame_count if frame_count is not None else "streamed"
    logger.debug("ffmpeg encode complete output=%s frames=%s duration=%.2fs", output_path, frame_count_label, time.perf_counter() - started_at)
    return _preset_metadata(preset_id, custom_options)


def _audio_codec_bitrate_from_args(args: list[str]) -> tuple[str, int | None]:
    codec = ""
    bitrate_kbps: int | None = None
    for idx, value in enumerate(args):
        if value == "-c:a" and idx + 1 < len(args):
            codec = str(args[idx + 1])
        elif value == "-b:a" and idx + 1 < len(args):
            raw = str(args[idx + 1]).strip().lower()
            try:
                if raw.endswith("k"):
                    bitrate_kbps = int(float(raw[:-1]))
                else:
                    bitrate_kbps = max(1, int(float(raw) / 1000.0))
            except (TypeError, ValueError):
                bitrate_kbps = None
    return codec, bitrate_kbps


def audio_only_export_spec(
    preset_id: str | None,
    custom_options: dict | None = None,
) -> dict:
    preset_id = normalize_save_preset(preset_id)
    if preset_id == CUSTOM_SAVE_VIDEO_PRESET:
        spec = resolve_custom_export_options(custom_options)
        codec = str(spec["audio_codec"])
        bitrate_kbps = int(spec["audio_bitrate_kbps"])
    else:
        audio_args = _preset_audio_args(preset_id, custom_options)
        codec, bitrate_kbps = _audio_codec_bitrate_from_args(audio_args)

    if codec == "none" or not codec:
        raise ValueError("Selected preset does not define an audio codec for audio-only export")
    if codec == "aac":
        extension = ".m4a"
        container = "m4a"
    elif codec == "pcm_s16le":
        extension = ".wav"
        container = "wav"
    elif codec == "flac":
        extension = ".flac"
        container = "flac"
    else:
        raise ValueError(f"Unsupported audio-only codec: {codec}")

    metadata = _preset_metadata(preset_id, custom_options)
    metadata.update({
        "codec": codec,
        "pix_fmt": "",
        "container": container,
        "extension": extension,
        "audio_only": True,
        "video_stream": False,
        "audio_mode": f"{codec} {bitrate_kbps}k".strip() if codec == "aac" and bitrate_kbps else codec,
    })
    return {
        "codec": codec,
        "container": container,
        "extension": extension,
        "bitrate_kbps": bitrate_kbps,
        "metadata": metadata,
    }


def encode_audio(
    input_wav: str,
    output_path: str,
    *,
    codec: str,
    container: str,
    bitrate_kbps: int | None = None,
    timeout: int | float | None = 90,
    cancel_event=None,
) -> None:
    codec = str(codec or "").strip()
    container = str(container or "").strip()
    if codec not in {"aac", "pcm_s16le", "flac"}:
        raise ValueError(f"Unsupported audio codec: {codec}")
    if container not in {"m4a", "wav", "flac"}:
        raise ValueError(f"Unsupported audio container: {container}")

    args = ["-c:a", codec]
    if codec == "aac":
        args += ["-b:a", f"{int(bitrate_kbps or 192)}k"]
    cmd = [
        get_ffmpeg_path(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(input_wav),
        "-vn",
        *args,
        str(output_path),
    ]
    try:
        run_ffmpeg_command(cmd, timeout=timeout, cancel_event=cancel_event)
    except MediaOperationCancelled:
        try:
            if os.path.isfile(output_path):
                os.remove(output_path)
        except OSError:
            pass
        raise
