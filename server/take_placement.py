"""Original take geometry in placement-clock frames, independent of encoding FPS.

Save Video retains its existing frame-for-frame mapping when encoding FPS differs.
Persist placement_fps separately for reconstruction of the original clock.

Restoration (dropping a take from Assets) recomputes the same geometry from the
asset's recorded metadata and converts it to the destination scene's clock. The
fixture table `tests/fixtures/take_restoration_cases.json` pins it, and any
browser mirror is held to the same table.
"""
import math

TAKE_PLACEMENT_POLICY = "authored_v1"


def calculate_take_placement(context: dict, encoded_frame_count: int) -> dict:
    def integer(key, default=0):
        try:
            return int(context.get(key, default) or 0)
        except (TypeError, ValueError, OverflowError):
            return default

    count = max(0, int(encoded_frame_count))
    padding = max(0, integer("frame_count_padding"))
    # Keep padding exposes the whole encoded tail, but only when the Editor's mask
    # reached the end of the tensor: otherwise the pad was never generated (the
    # repeated last frame sits after post-context, outside the mask). With zero
    # padding it changes nothing.
    keep_padding = (context.get("take_keep_padding") is True and padding > 0
                    and integer("mask_end_frame", -1) >= integer("frame_count", count))
    available = count if keep_padding else max(0, count - padding)
    start = integer("selection_start")
    end = integer("selection_end", start + count)
    pre = max(0, integer("actual_pre_context_frames",
                         integer("pre_context_frames", integer("context_frames"))))
    post = max(0, integer("actual_post_context_frames",
                          integer("post_context_frames", integer("context_frames"))))
    # Retire defaults when all live execution-context producers carry requested_*.
    # Historical asset restoration must resolve its policy before using this math.
    before = min(pre, max(0, integer("requested_mask_pre_offset", integer("mask_pre_offset"))))
    after = min(post, max(0, integer("requested_mask_post_offset", integer("mask_post_offset"))))
    if context.get("take_placement_mode") == "untrimmed":
        source_in, source_out = 0, available
        timeline_start = start - pre
    else:
        source_in = min(available, pre - before)
        source_out = available if keep_padding else max(
            source_in, min(available, pre + max(0, end - start) + after))
        timeline_start = start - before
    return {
        "source_in_frame": source_in,
        "source_out_frame": source_out,
        "total_source_frames": available,
        "source_origin_frame": 0,
        "timeline_start_frame": timeline_start,
        "timeline_end_frame": timeline_start + source_out - source_in,
    }


def _finite_positive(value) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def _whole(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return int(number) if math.isfinite(number) and number == int(number) else None


def take_restoration_source(params, asset_type: str) -> dict:
    """What a dropped asset's provenance says about restoring its placement.

    ``status`` is one of:

    - ``"supported"``: a marked take, with ``scene_id``, ``placement_fps``, the
      ``context`` `calculate_take_placement` reads and, for audio, the recorded
      ``encoded_frame_count`` (video uses its own media frame count);
    - ``"legacy"``: a Save Video take saved before takes recorded their placement
      clock. Nothing recorded says what FPS its frame numbers count in (the file's
      FPS is Save Video's independently supplied encoding rate), so it is never
      restored; the drop explains why;
    - ``"unavailable"``: marked, but the policy is unknown or the record is
      incomplete;
    - ``"not_take"``: nothing to restore; an ordinary drop, silently.
    """
    if asset_type not in {"video", "audio"} or not isinstance(params, dict):
        return {"status": "not_take"}
    policy = params.get("take_placement_policy")
    export = params.get("editor_export")
    export_range = export.get("range") if isinstance(export, dict) else None
    saved_take = "selection_start" in params and bool(params.get("scene_id"))
    if policy is None:
        return {"status": "legacy", "reason": "legacy"} if saved_take else {"status": "not_take"}
    if policy != TAKE_PLACEMENT_POLICY:
        return {"status": "unavailable", "reason": "unsupported_policy"}
    placement_fps = _finite_positive(params.get("placement_fps"))
    if not placement_fps:
        return {"status": "unavailable", "reason": "missing_fps"}
    if saved_take:
        scene_id = str(params.get("scene_id") or "")
        start, end = _whole(params.get("selection_start")), _whole(params.get("selection_end"))
        context = params
    elif isinstance(export_range, dict):
        scene_id = str(export.get("scene_id") or "")
        start, end = _whole(export_range.get("start")), _whole(export_range.get("end"))
        context = {"selection_start": start, "selection_end": end}
    else:
        return {"status": "unavailable", "reason": "incomplete"}
    if not scene_id or start is None or end is None or end <= start:
        return {"status": "unavailable", "reason": "incomplete"}
    source = {"status": "supported", "scene_id": scene_id,
              "placement_fps": placement_fps, "context": context}
    if asset_type == "audio":
        encoded = _whole(params.get("encoded_frame_count"))
        if encoded is None or encoded <= 0:
            return {"status": "unavailable", "reason": "incomplete"}
        source["encoded_frame_count"] = encoded
    return source


def _half_up(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def restore_take_geometry(source: dict, encoded_frame_count: int, *, same_scene: bool,
                          destination_fps: float, cursor_frame: int) -> dict:
    """The restored geometry in destination-scene frames, or ``{"reason": ...}``.

    The original scene restores its original time; another scene places the
    saved trim at the cursor. At another FPS, source in/out are converted half-up
    once and their difference is the one visible duration; the timeline end is
    the start plus that duration, never an independently rounded second span. A
    positive range that collapses below one frame keeps one frame while a source
    frame remains. Source origin stays zero so hidden context stays reachable.
    """
    geometry = calculate_take_placement(source["context"], encoded_frame_count)
    if (geometry["timeline_end_frame"] <= geometry["timeline_start_frame"]
            or geometry["total_source_frames"] <= 0):
        return {"reason": "empty"}
    destination_fps = _finite_positive(destination_fps)
    if not destination_fps:
        return {"reason": "missing_fps"}
    ratio = destination_fps / source["placement_fps"]
    same_rate = math.isclose(ratio, 1.0, rel_tol=1e-9, abs_tol=0.0)

    def convert(frame):
        return int(frame) if same_rate else _half_up(frame * ratio)

    source_in = convert(geometry["source_in_frame"])
    source_out = convert(geometry["source_out_frame"])
    available = convert(geometry["total_source_frames"])
    if source_in >= available:
        return {"reason": "collapsed"}
    if source_out - source_in < 1:
        source_out = source_in + 1
    start = convert(geometry["timeline_start_frame"]) if same_scene else int(cursor_frame)
    if start < 0:
        return {"reason": "before_scene_start"}
    return {
        "source_in_frame": source_in,
        "source_out_frame": source_out,
        "total_source_frames": available,
        "source_origin_frame": 0,
        "timeline_start_frame": start,
        "timeline_end_frame": start + source_out - source_in,
    }
