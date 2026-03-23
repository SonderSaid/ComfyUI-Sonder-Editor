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
