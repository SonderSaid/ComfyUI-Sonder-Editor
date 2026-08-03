"""Pure effective-Reference resolution shared conceptually with the frontend."""

from __future__ import annotations


# Bridge output names in tuple order, plus the "slots" pseudo-name covering the
# whole r01..r16 block. Mirrored in web/js/reference_resolution.js.
REFERENCE_OUTPUT_NAMES = (
    "reference_frames",
    "reference_idx",
    "reference_strength",
    "reference_audio",
    "reference_prompt",
    "reference_names",
    "context",
    "slots",
)


def reference_live_outputs(hard) -> set:
    """Return the outputs a recipe actually drives.

    A recipe without a `live_outputs` declaration (detached/custom) keeps every
    output live; anything else is dead and emits its documented fallback.
    """
    declared = hard.get("live_outputs") if isinstance(hard, dict) else None
    if not isinstance(declared, list):
        return set(REFERENCE_OUTPUT_NAMES)
    live = {str(name) for name in declared if isinstance(name, str)}
    return live.intersection(REFERENCE_OUTPUT_NAMES)


def _integer(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def resolve_effective_references(
    *,
    reference_items,
    lane_count,
    scene_duration,
    window_start,
    window_end,
    lane_configs=None,
) -> list:
    """Return one most-specific overlapping item per Reference lane.

    Ranges are half-open. A negative item end resolves to scene end before
    scoring. Muted items and hidden lanes do not participate.
    """
    count = max(1, _integer(lane_count, 1))
    duration = max(0, _integer(scene_duration, 0))
    start = min(duration, max(0, _integer(window_start, 0)))
    end = min(duration, max(start, _integer(window_end, duration)))
    configs = lane_configs if isinstance(lane_configs, list) else []
    items = reference_items if isinstance(reference_items, list) else []
    winners = [None] * count
    scores = [None] * count

    for item_index, item in enumerate(items):
        if isinstance(item, dict):
            value = item
            get = value.get
        else:
            value = item
            get = lambda key, default=None: getattr(value, key, default)
        lane_index = _integer(get("lane_index", 0), 0)
        if lane_index < 0 or lane_index >= count or bool(get("muted", False)):
            continue
        if lane_index < len(configs):
            config = configs[lane_index]
            hidden = config.get("hidden", False) if isinstance(config, dict) else getattr(config, "hidden", False)
            if bool(hidden):
                continue
        item_start = min(max(0, _integer(get("start_frame", 0), 0)), max(0, duration - 1))
        raw_end = _integer(get("end_frame", -1), -1)
        item_end = duration if raw_end < 0 else min(duration, raw_end)
        if item_end <= item_start:
            continue
        overlap = max(0, min(item_end, end) - max(item_start, start))
        if overlap <= 0:
            continue
        score = (overlap / (item_end - item_start), item_start, item_index)
        if scores[lane_index] is None or score > scores[lane_index]:
            scores[lane_index] = score
            winners[lane_index] = {"laneIndex": lane_index, "item": item}
    return winners
