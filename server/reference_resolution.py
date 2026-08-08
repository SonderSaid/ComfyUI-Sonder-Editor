"""Pure effective-Reference resolution shared conceptually with the frontend."""

from __future__ import annotations


# Type-homogeneous Bridge output groups. Mirrored in
# web/js/reference_resolution.js. Numbered socket names are presentation detail;
# recipes declare which whole bridge block they drive.
REFERENCE_OUTPUT_NAMES = (
    "image_slots",
    "audio_slots",
    "reference_prompt",
    "reference_names",
)

_RETIRED_REFERENCE_OUTPUT_NAMES = {
    "reference_frames": "image_slots",
    "slots": "image_slots",
    "reference_audio": "audio_slots",
    "reference_prompt": "reference_prompt",
    "reference_names": "reference_names",
    # These values moved to the Selector or were deliberately retired.
    "reference_idx": None,
    "reference_strength": None,
    "context": None,
}


def migrate_live_outputs(hard) -> dict:
    """Rewrite the pre-split Bridge vocabulary without losing authoring.

    Unknown values are retained so route validation can still refuse typos. If
    every declared value retired, the declaration is removed: an empty set must
    fail open to the documented all-live default, not turn a legacy recipe into
    one that drives nothing.
    """
    result = dict(hard) if isinstance(hard, dict) else {}
    declared = result.get("live_outputs")
    if not isinstance(declared, list):
        return result
    migrated = []
    for raw_name in declared:
        if not isinstance(raw_name, str):
            migrated.append(raw_name)
            continue
        mapped = _RETIRED_REFERENCE_OUTPUT_NAMES.get(raw_name, raw_name)
        if mapped is not None and mapped not in migrated:
            migrated.append(mapped)
    if migrated:
        result["live_outputs"] = migrated
    else:
        result.pop("live_outputs", None)
    return result


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
    frame_threshold_pct=0.0,
) -> list:
    """Return one most-specific overlapping item per Reference lane.

    Ranges are half-open. A negative item end resolves to scene end before
    scoring. Muted items and hidden lanes do not participate.

    `frame_threshold_pct` drops an item whose in-window overlap is under that
    percentage of its OWN span, before the survivors are scored. Unlike the
    prompt boundary threshold there is no never-empty guard: References resolve
    to a single winner per lane, so a lane may legitimately resolve to nothing
    and report `has_reference = 0` for that window. That is the point of the
    setting, and it is why enqueue warns when it flips across a batch.

    Enqueue announces BOTH causes of that flip, under separate notifications:
    the threshold dropping a chunk the item does overlap, and the item simply
    not reaching a chunk at all. Only the first is a setting the user probably
    did not mean to hit, but both change which sockets are wired and so both
    change the inferred task mode. The frontend keeps the reason — see
    `classifyReferenceChunks` in `web/js/reference_resolution.js`; this module
    resolves winners only and has no verdict concept.
    """
    count = max(1, _integer(lane_count, 1))
    duration = max(0, _integer(scene_duration, 0))
    start = min(duration, max(0, _integer(window_start, 0)))
    end = min(duration, max(start, _integer(window_end, duration)))
    configs = lane_configs if isinstance(lane_configs, list) else []
    items = reference_items if isinstance(reference_items, list) else []
    try:
        threshold = max(0.0, min(100.0, float(frame_threshold_pct or 0.0))) / 100.0
    except (TypeError, ValueError):
        threshold = 0.0
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
        coverage = overlap / (item_end - item_start)
        if threshold > 0 and coverage < threshold:
            continue
        score = (coverage, item_start, item_index)
        if scores[lane_index] is None or score > scores[lane_index]:
            scores[lane_index] = score
            winners[lane_index] = {"laneIndex": lane_index, "item": item}
    return winners
