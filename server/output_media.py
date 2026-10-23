"""Normalize what an output node published into media entries Sonder can show.

A node's UI result (``executed.output`` / history ``outputs[node]``) is a dict of
channels, each a list of file descriptors. The channel name says nothing reliable
about the media: native Save Video publishes MP4s under ``images``. So the kind
comes from the file's container, the channel only picks the adapter.

Every entry names a file ComfyUI's ``/view`` serves (``filename``, ``subfolder``,
``type``); nothing here reads the disk. A descriptor that is not a plain relative
name under ``output``/``temp``/``input`` is dropped, and any URL, absolute path or
extra field a publisher adds is ignored. A channel no adapter knows becomes one
``unsupported`` entry, so a View can say so instead of guessing.

Pure and stdlib-only.
"""

from __future__ import annotations

import math
import os
from typing import Any

FILE_TYPES = frozenset({"output", "temp", "input"})
MAX_ENTRIES_PER_NODE = 64
MAX_ENTRIES_PER_RUN = 256
MAX_NAME_CHARS = 255
MAX_SUBFOLDER_CHARS = 512

_VIDEO = {".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
          ".mkv": "video/x-matroska", ".m4v": "video/mp4"}
_ANIMATABLE = {".webp": "image/webp", ".gif": "image/gif", ".apng": "image/apng", ".png": "image/png"}
_IMAGE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
          ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
          ".exr": "image/x-exr"}
_AUDIO = {".wav": "audio/wav", ".flac": "audio/flac", ".mp3": "audio/mpeg", ".ogg": "audio/ogg",
          ".opus": "audio/opus", ".m4a": "audio/mp4", ".aac": "audio/aac"}
_SCENE_MAP_FIELDS = ("scene_start", "scene_end", "source_offset")


def _clean_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_NAME_CHARS:
        return None
    if value in {".", ".."} or any(ch in value for ch in "/\\:\x00") or "%2f" in value.lower() or "%5c" in value.lower():
        return None
    return value


def _clean_subfolder(value: Any) -> str | None:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > MAX_SUBFOLDER_CHARS:
        return None
    if "\x00" in value or ":" in value or "%2f" in value.lower() or "%5c" in value.lower():
        return None
    if value.startswith(("/", "\\")):
        return None
    parts = value.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return None
    return "/".join(parts)


def clean_file(descriptor: Any) -> dict | None:
    """``{filename, subfolder, type}`` when the descriptor names a servable file."""
    if not isinstance(descriptor, dict):
        return None
    filename = _clean_name(descriptor.get("filename"))
    subfolder = _clean_subfolder(descriptor.get("subfolder"))
    file_type = descriptor.get("type") or "output"
    if filename is None or subfolder is None or file_type not in FILE_TYPES:
        return None
    return {"filename": filename, "subfolder": subfolder, "type": file_type}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None


def _ext(filename: str) -> str:
    return os.path.splitext(filename)[1].lower()


def _scene_map(value: Any) -> dict | None:
    if not isinstance(value, dict) or not isinstance(value.get("scene_id"), str):
        return None
    out = {"scene_id": value["scene_id"][:128]}
    for name in _SCENE_MAP_FIELDS:
        number = value.get(name)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            return None
        out[name] = number
    fps = _number(value.get("fps"))
    if fps is not None:
        out["fps"] = fps
    return out if out["scene_end"] > out["scene_start"] else None


def _entry(source: str, real_node: str, channel: str, index: int, kind: str, file: dict | None, mime: str = "", **extra: Any) -> dict:
    entry = {"source": source, "real_node": real_node, "channel": channel, "index": index, "kind": kind, "file": file, "mime": mime}
    entry.update({key: value for key, value in extra.items() if value is not None})
    return entry


def _image_like(source, real_node, channel, items, animated: bool) -> list[dict]:
    entries = []
    for index, item in enumerate(items):
        file = clean_file(item)
        if file is None:
            continue
        ext = _ext(file["filename"])
        if ext in _VIDEO:
            entries.append(_entry(source, real_node, channel, index, "video", file, _VIDEO[ext]))
        elif animated and ext in _ANIMATABLE:
            entries.append(_entry(source, real_node, channel, index, "animated_image", file, _ANIMATABLE[ext]))
        elif ext in _IMAGE:
            entries.append(_entry(source, real_node, channel, index, "image", file, _IMAGE[ext]))
        elif ext in _AUDIO:
            entries.append(_entry(source, real_node, channel, index, "audio", file, _AUDIO[ext]))
    return entries


def _audio(source, real_node, channel, items) -> list[dict]:
    entries = []
    for index, item in enumerate(items):
        file = clean_file(item)
        if file is not None and _ext(file["filename"]) in _AUDIO:
            entries.append(_entry(source, real_node, channel, index, "audio", file, _AUDIO[_ext(file["filename"])]))
    return entries


def _vhs(source, real_node, channel, items) -> list[dict]:
    # VideoHelperSuite Video Combine: {filename, subfolder, type, format, frame_rate}.
    # Its ``fullpath`` is an absolute server path and is never carried.
    entries = []
    for index, item in enumerate(items):
        file = clean_file(item)
        if file is None:
            continue
        ext, fmt = _ext(file["filename"]), str(item.get("format") or "")
        fps = _number(item.get("frame_rate"))
        if ext in _VIDEO or fmt.startswith("video/"):
            entries.append(_entry(source, real_node, channel, index, "video", file, _VIDEO.get(ext, fmt[:64]), fps=fps))
        elif ext in _ANIMATABLE:
            entries.append(_entry(source, real_node, channel, index, "animated_image", file, _ANIMATABLE[ext], fps=fps))
    return entries


def _sonder_video(source, real_node, channel, items) -> list[dict]:
    entries = []
    for index, item in enumerate(items):
        file = clean_file(item)
        if file is None or _ext(file["filename"]) not in _VIDEO:
            continue
        poster = clean_file(item.get("poster"))
        entries.append(_entry(
            source, real_node, channel, index, "video", file, _VIDEO[_ext(file["filename"])],
            fps=_number(item.get("fps")),
            has_audio=bool(item.get("has_audio")) if "has_audio" in item else None,
            poster=poster,
            scene_map=_scene_map(item.get("scene_map")),
        ))
    return entries


_ADAPTERS = {
    "audio": lambda s, r, c, items, _ui: _audio(s, r, c, items),
    "gifs": lambda s, r, c, items, _ui: _vhs(s, r, c, items),
    "sonder_video": lambda s, r, c, items, _ui: _sonder_video(s, r, c, items),
}
# Channels that carry no media of their own: ``animated`` qualifies ``images``.
_SIDE_CHANNELS = frozenset({"animated"})


def _animated(ui: dict) -> bool:
    flags = ui.get("animated")
    if isinstance(flags, (list, tuple)):
        return any(flag is True for flag in flags)
    return flags is True


def normalize(source: Any, real_node: Any, ui_output: Any) -> tuple[list[dict], int]:
    """Entries for one node's published UI, plus how many were dropped by the cap."""
    if not isinstance(ui_output, dict):
        return [], 0
    source, real_node = str(source or ""), str(real_node or source or "")
    entries: list[dict] = []
    for channel, items in ui_output.items():
        channel = str(channel)
        if channel in _SIDE_CHANNELS:
            continue
        if not isinstance(items, (list, tuple)):
            continue
        if channel == "images":
            found = _image_like(source, real_node, channel, items, _animated(ui_output))
        elif channel in _ADAPTERS:
            found = _ADAPTERS[channel](source, real_node, channel, items, ui_output)
        else:
            found = [_entry(source, real_node, channel[:64], 0, "unsupported", None)] if items else []
        entries.extend(found)
    dropped = max(0, len(entries) - MAX_ENTRIES_PER_NODE)
    return entries[:MAX_ENTRIES_PER_NODE], dropped


def file_identity(entry: dict) -> tuple | None:
    file = entry.get("file") if isinstance(entry, dict) else None
    if not isinstance(file, dict):
        return None
    return file.get("type"), file.get("subfolder"), file.get("filename")
