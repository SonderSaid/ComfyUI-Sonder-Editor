"""Output Views timing: an authored View's ``timing`` and the content digest of
what an Editor run was generated from.

``timing`` (Relay ``views.items.<id>.timing``) says how an output's frames sit
on the scene that fed it, for outputs with no trusted placement of their own
(a Save Video take carries ``scene_map``). ``clean_timing`` is the one reader;
``web/js/view_timing.js`` ``normalizeTiming`` mirrors it.

The **content record** lets a linked pane say "Previous context": the scene
changed in the output's range after it was made. The Editor records, at
execution, a digest of the inputs it consumed inside its window (render clips,
Driver clips, effective guides, audio tracks, lane visibility, fps and size)
plus the ids of the clips and audio tracks present. The browser builds the same
projection from the scene it holds and compares (``contentStatus`` in
view_timing.js); nothing is read from the server for it. The two projections
must stay byte-identical, so the canonical text is tuples in a fixed order and
the hash is the two-lane 32-bit hash ``web/js/text_hash.js`` uses, over UTF-16
code units. ``tests/test_view_timing_js.py`` pins the parity.

Pure and standard-library only.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

TIMING_MODES = ("editor_window",)
TIMING_ANCHORS = ("render_start", "generation_start")
MAX_SOURCE_OFFSET = 1_000_000
MAX_FPS = 240.0
CONTENT_VERSION = 1
# Clip and audio-track ids recorded per run; past it the record is omitted
# (the pane then shows no Previous-context mark, never a wrong one).
MAX_PRESENT = 256


# ── Timing ───────────────────────────────────────────────────────────────────

def _whole(value: Any) -> Any:
    # JavaScript reads 5.0 as 5: a whole float is that whole number here too.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def clean_timing(raw: Any) -> dict | None:
    """A View's timing, or None when absent or invalid (read as unlinked)."""
    if not isinstance(raw, dict):
        return None
    if raw.get("mode") not in TIMING_MODES or raw.get("anchor") not in TIMING_ANCHORS:
        return None
    offset = _whole(raw.get("source_offset", 0))
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= MAX_SOURCE_OFFSET:
        return None
    fps = raw.get("fps", "editor")
    if fps != "editor":
        fps = _whole(fps)
        if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or not 0 < fps <= MAX_FPS:
            return None
    return {"mode": raw["mode"], "anchor": raw["anchor"], "source_offset": offset, "fps": fps}


# ── Canonical text and hash ──────────────────────────────────────────────────

def _utf16_units(text: str) -> list[int]:
    data = text.encode("utf-16-le", "surrogatepass")
    return [data[i] | (data[i + 1] << 8) for i in range(0, len(data), 2)]


def two_lane_hash(text: str) -> str:
    """``web/js/text_hash.js`` ``twoLaneHash``: two 32-bit FNV-style lanes over
    UTF-16 code units, as 16 hex digits."""
    a, b = 0x811C9DC5, 0x9E3779B9
    for unit in _utf16_units(text):
        a = ((a ^ unit) * 0x01000193) & 0xFFFFFFFF
        b = ((b ^ unit) * 0x85EBCA6B) & 0xFFFFFFFF
    return f"{a:08x}{b:08x}"


def micro(value: Any) -> int:
    """A float as whole millionths; non-finite reads -1 (both sides)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return -1
    if not math.isfinite(number):
        return -1
    return int(math.floor(number * 1e6 + 0.5))


def canonical(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return f"{len(_utf16_units(value))}:{value}"
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical(item) for item in value) + "]"
    raise TypeError(f"Not canonical: {type(value).__name__}")


# ── Content projection ───────────────────────────────────────────────────────

def _get(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        value = item.get(name, default)
    else:
        value = getattr(item, name, default)
    return default if value is None else value


def _int(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return int(math.floor(number + 0.5)) if math.isfinite(number) else 0


def normalize_source(path: Any) -> str:
    """A project-relative source path with ``/`` separators and no leading
    ``./``. Case is kept: both sides read the same project data, and case
    mapping differs between Python and JavaScript (final sigma)."""
    value = str(path or "").replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value


def _hidden(configs: Any, index: int) -> bool:
    configs = configs or []
    if not 0 <= index < len(configs):
        return False
    return bool(_get(configs[index], "hidden", False))


def _overlap(item: Any, window: tuple[int, int]) -> tuple[int, int, int] | None:
    start, end = _int(_get(item, "timeline_start_frame", 0)), _int(_get(item, "timeline_end_frame", 0))
    lo, hi = max(start, window[0]), min(end, window[1])
    if hi <= lo:
        return None
    return lo, hi, _int(_get(item, "source_in_frame", 0)) + lo - start


def _src_key(src: str) -> list[int]:
    return _utf16_units(src)


def _clip_rows(clips: Iterable[Any], window, configs, *, strength: bool) -> list[tuple]:
    rows = []
    for clip in clips:
        found = _overlap(clip, window)
        if found is None:
            continue
        lane = _int(_get(clip, "track_index", 0))
        src = normalize_source(_get(clip, "source_path", ""))
        rows.append((lane, _hidden(configs, lane), src, found[0], found[1], found[2],
                     micro(_get(clip, "strength" if strength else "opacity", 1.0)), bool(_get(clip, "muted", False)),
                     str(_get(clip, "fit_mode", "pad_edge") or "pad_edge"),
                     str(_get(clip, "crop_position", "center") or "center")))
    rows.sort(key=lambda row: (row[0], row[3], row[4], _src_key(row[2])))
    return rows


def _audio_rows(tracks: Iterable[Any], window, configs) -> list[tuple]:
    rows = []
    for track in tracks:
        found = _overlap(track, window)
        if found is None:
            continue
        lane = _int(_get(track, "lane_index", 0))
        src = normalize_source(_get(track, "source_path", ""))
        rows.append((lane, _hidden(configs, lane), src, found[0], found[1], found[2],
                     micro(_get(track, "volume", 1.0)), bool(_get(track, "muted", False))))
    rows.sort(key=lambda row: (row[0], row[3], row[4], _src_key(row[2])))
    return rows


def _guide_rows(guides: Iterable[Any], window, *, track_hidden: bool, duration: int, fit: bool) -> list[tuple]:
    rows = []
    if track_hidden:
        return rows
    for guide in guides:
        if bool(_get(guide, "muted", False)):
            continue
        at = _int(_get(guide, "frame_index", 0))
        if at == -1:
            at = duration - 1
        if not window[0] <= at < window[1]:
            continue
        row = [at, str(_get(guide, "asset_id", "") or ""), micro(_get(guide, "strength", 1.0))]
        if fit:
            row += [str(_get(guide, "fit_mode", "pad_edge") or "pad_edge"), str(_get(guide, "crop_position", "center") or "center")]
        rows.append(tuple(row))
    rows.sort(key=lambda row: (row[0], _src_key(row[1]), row[2:]))
    return rows


def content_text(*, fps: float, width: int, height: int, window, input_window, guide_fit: bool,
                 render_clips, render_configs, driver_clips, driver_configs, guides, guide_track_hidden: bool,
                 duration: int, audio_tracks, audio_configs) -> str:
    """The canonical text of one run's inputs; ``contentText`` in view_timing.js."""
    window, input_window = (int(window[0]), int(window[1])), (int(input_window[0]), int(input_window[1]))
    return canonical(("v1", micro(fps), _int(width), _int(height), bool(guide_fit),
                      _clip_rows(render_clips, window, render_configs, strength=False),
                      _clip_rows(driver_clips, input_window, driver_configs, strength=True),
                      _guide_rows(guides, input_window, track_hidden=guide_track_hidden, duration=_int(duration), fit=guide_fit),
                      _audio_rows(audio_tracks, window, audio_configs)))


def content_record(*, fps: float, width: int, height: int, window, input_window, guide_fit: bool,
                   render_clips, render_configs, driver_clips, driver_configs, guides, guide_track_hidden: bool,
                   duration: int, audio_tracks, audio_configs) -> dict | None:
    """``editor_context.content``: the digest and the ids present at execution,
    or None past ``MAX_PRESENT``."""
    render_clips, driver_clips, audio_tracks = list(render_clips or []), list(driver_clips or []), list(audio_tracks or [])
    window = (int(window[0]), int(window[1]))
    input_window = (max(window[0], int(input_window[0])), min(window[1], int(input_window[1])))
    if input_window[1] < input_window[0]:
        input_window = (window[0], window[0])
    present = sorted({str(_get(item, "clip_id", "") or "") for item in render_clips if _overlap(item, window)}
                     | {str(_get(item, "clip_id", "") or "") for item in driver_clips if _overlap(item, input_window)}
                     | {str(_get(item, "track_id", "") or "") for item in audio_tracks if _overlap(item, window)})
    present = [identity for identity in present if identity]
    if len(present) > MAX_PRESENT:
        return None
    text = content_text(fps=fps, width=width, height=height, window=window, input_window=input_window, guide_fit=guide_fit,
                        render_clips=render_clips, render_configs=render_configs, driver_clips=driver_clips,
                        driver_configs=driver_configs, guides=guides, guide_track_hidden=guide_track_hidden,
                        duration=duration, audio_tracks=audio_tracks, audio_configs=audio_configs)
    return {"v": CONTENT_VERSION, "digest": two_lane_hash(text), "present": present,
            "window": list(window), "input_window": list(input_window), "guide_fit": bool(guide_fit)}


def scene_content_record(scene: Any, *, fps: float, width: int, height: int, render_start: int, render_end: int,
                         driver_source: Iterable[Any] | None, guide_frames: Iterable[Any] | None,
                         guide_track_hidden: bool, snapshot_window: tuple[int, int] | None,
                         driver_configs: Iterable[Any] | None = None,
                         snapshot_guide_fit: bool = False) -> dict | None:
    """The Editor's content record for one run. A queued snapshot run took its
    guides, Drivers and Driver lane visibility from the job (``snapshot_window``,
    ``driver_configs``); its render clips and audio are the live scene's at
    execution. Its guides carry fit and crop only when the job's raw snapshot
    entries recorded them (``snapshot_guide_fit``); older jobs' never did, and
    their guides rendered with the defaults."""
    window = (render_start, render_end)
    return content_record(
        fps=fps, width=width, height=height, window=window, input_window=snapshot_window or window,
        guide_fit=snapshot_window is None or bool(snapshot_guide_fit),
        render_clips=[c for c in (_get(scene, "clips", []) or []) if _get(c, "role", "render") != "motion_driver"],
        render_configs=_get(scene, "video_lane_configs", []),
        driver_clips=[c for c in (driver_source or []) if _get(c, "role", "render") == "motion_driver"],
        driver_configs=list(driver_configs) if driver_configs is not None else _get(scene, "motion_driver_lane_configs", []),
        guides=list(guide_frames or []), guide_track_hidden=bool(guide_track_hidden),
        duration=_int(_get(scene, "duration_frames", 0)),
        audio_tracks=_get(scene, "audio_tracks", []) or [],
        audio_configs=_get(scene, "audio_lane_configs", []),
    )
