"""Thumbnail generation for project assets."""

import logging
import os

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger("ltx_editor")

THUMB_SIZE = (192, 128)  # width, height


def generate_video_thumbnail(video_path: str, output_path: str,
                             size: tuple[int, int] = THUMB_SIZE) -> bool:
    """Extract first frame from video and save as PNG thumbnail."""
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return False
        ret, frame = cap.read()
        cap.release()
        if not ret:
            return False

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        img = Image.fromarray(rgb)
        img.thumbnail(size, Image.Resampling.LANCZOS)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        img.save(output_path, "PNG")
        return True
    except Exception as e:
        logger.warning("Failed to generate video thumbnail for %s: %s", video_path, e)
        return False


def generate_image_thumbnail(image_path: str, output_path: str,
                             size: tuple[int, int] = THUMB_SIZE) -> bool:
    """Resize image and save as PNG thumbnail."""
    try:
        img = Image.open(image_path)
        img.thumbnail(size, Image.Resampling.LANCZOS)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        img.save(output_path, "PNG")
        return True
    except Exception as e:
        logger.warning("Failed to generate image thumbnail for %s: %s", image_path, e)
        return False


def generate_audio_waveform(audio_path: str, output_path: str,
                            size: tuple[int, int] = (192, 64)) -> bool:
    """Generate a simple waveform visualization as a PNG thumbnail."""
    try:
        import torchaudio
        waveform, sample_rate = torchaudio.load(audio_path)
        # Mix to mono
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        samples = waveform.squeeze().numpy()

        w, h = size
        img = np.zeros((h, w, 3), dtype=np.uint8)
        img[:] = (40, 40, 40)  # dark gray background

        # Downsample to width
        chunk_size = max(1, len(samples) // w)
        for x in range(min(w, len(samples) // chunk_size)):
            chunk = samples[x * chunk_size:(x + 1) * chunk_size]
            peak = min(float(np.abs(chunk).max()), 1.0)
            bar_h = int(peak * (h // 2))
            center = h // 2
            # Draw waveform bar (cyan-ish color)
            img[center - bar_h:center + bar_h, x] = (100, 200, 220)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        pil_img = Image.fromarray(img)
        pil_img.save(output_path, "PNG")
        return True
    except Exception as e:
        logger.warning("Failed to generate audio waveform for %s: %s", audio_path, e)
        return False


def generate_thumbnail_strip(video_path: str, output_path: str,
                             num_frames: int = 20, frame_height: int = 28) -> dict | None:
    """Extract evenly-spaced frames from video and stitch into a horizontal sprite strip.

    Returns dict with strip metadata: {frame_width, num_frames, total_width, frame_height}
    or None on failure.
    """
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if total_frames <= 0 or orig_h <= 0:
            cap.release()
            return None

        actual_num = min(num_frames, total_frames)
        frame_w = max(1, int(orig_w * frame_height / orig_h))

        frames = []
        for i in range(actual_num):
            frame_idx = int(i * total_frames / actual_num)
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret:
                # Use a black frame as fallback
                frames.append(np.zeros((frame_height, frame_w, 3), dtype=np.uint8))
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb)
            img = img.resize((frame_w, frame_height), Image.Resampling.LANCZOS)
            frames.append(np.array(img))

        cap.release()

        if not frames:
            return None

        # Stitch horizontally
        strip = np.concatenate(frames, axis=1)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        Image.fromarray(strip).save(output_path, "JPEG", quality=80)

        return {
            "frame_width": frame_w,
            "num_frames": actual_num,
            "total_width": frame_w * actual_num,
            "frame_height": frame_height,
        }
    except Exception as e:
        logger.warning("Failed to generate thumbnail strip for %s: %s", video_path, e)
        return None


def generate_waveform_data(audio_path: str, output_path: str,
                           num_buckets: int = 500) -> dict | None:
    """Generate waveform peaks data as JSON for timeline rendering.

    Returns dict with peaks data or None on failure.
    """
    samples = None
    sample_rate = 44100

    # Method 1: wave module (WAV files only)
    try:
        import wave
        with wave.open(audio_path, 'rb') as wf:
            sample_rate = wf.getframerate()
            n_channels = wf.getnchannels()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
            dtype = np.int16 if wf.getsampwidth() == 2 else np.int32
            arr = np.frombuffer(raw, dtype=dtype).astype(np.float32)
            if dtype == np.int16:
                arr /= 32768.0
            else:
                arr /= 2147483648.0
            if n_channels > 1:
                arr = arr.reshape(-1, n_channels).mean(axis=1)
            samples = arr
    except Exception:
        pass

    # Method 2: torchaudio
    if samples is None:
        try:
            import torchaudio
            waveform, sr = torchaudio.load(audio_path)
            sample_rate = sr
            if waveform.shape[0] > 1:
                waveform = waveform.mean(dim=0, keepdim=True)
            samples = waveform.squeeze().numpy()
        except Exception:
            pass

    # Method 3: soundfile (handles FLAC, OGG, WAV natively)
    if samples is None:
        try:
            import soundfile as sf
            data, sr = sf.read(audio_path, dtype='float32')
            sample_rate = sr
            if data.ndim > 1:
                data = data.mean(axis=1)
            samples = data
        except Exception:
            pass

    # Method 4: ffmpeg decode to raw PCM (universal fallback)
    if samples is None:
        try:
            import subprocess
            result = subprocess.run(
                ["ffmpeg", "-i", audio_path, "-f", "s16le", "-ac", "1",
                 "-ar", "44100", "-"],
                capture_output=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout:
                samples = np.frombuffer(result.stdout, dtype=np.int16).astype(np.float32) / 32768.0
        except Exception:
            pass

    if samples is None or len(samples) == 0:
        return None

    try:
        actual_buckets = min(num_buckets, len(samples))
        chunk_size = max(1, len(samples) // actual_buckets)
        peaks = []
        for i in range(actual_buckets):
            chunk = samples[i * chunk_size:(i + 1) * chunk_size]
            if len(chunk) == 0:
                peaks.append([0.0, 0.0])
            else:
                peaks.append([float(chunk.min()), float(chunk.max())])

        import json
        data = {
            "peaks": peaks,
            "num_buckets": actual_buckets,
            "sample_rate": int(sample_rate),
        }
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(data, f)
        return data
    except Exception as e:
        logger.warning("Failed to generate waveform data for %s: %s", audio_path, e)
        return None


def ensure_thumbnail(asset_type: str, source_path: str, output_path: str) -> bool:
    """Generate thumbnail if it doesn't already exist. Returns True if thumbnail exists."""
    if os.path.isfile(output_path):
        return True

    if asset_type == "video":
        return generate_video_thumbnail(source_path, output_path)
    elif asset_type == "image":
        return generate_image_thumbnail(source_path, output_path)
    elif asset_type == "audio":
        return generate_audio_waveform(source_path, output_path)
    return False
