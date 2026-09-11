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


REFERENCE_VERDICT = {
    "WINNER": "winner", "SUPERSEDED": "superseded",
    "BELOW_THRESHOLD": "below_threshold", "OUTSIDE": "outside", "EXCLUDED": "excluded",
}
REFERENCE_VERDICT_LABEL = {
    "winner": "In window", "superseded": "Superseded",
    "below_threshold": "Below threshold", "outside": "Outside window", "excluded": "Excluded",
}


def _item_field(item, key, default=None):
    return item.get(key, default) if isinstance(item, dict) else getattr(item, key, default)


def resolve_reference_verdicts(
    *,
    reference_items,
    lane_count,
    scene_duration,
    window_start,
    window_end,
    lane_configs=None,
    frame_threshold_pct=0.0,
) -> dict:
    """Return one most-specific overlapping item per Reference lane.

    Ranges are half-open. A negative item end resolves to scene end before
    scoring. Muted items and hidden lanes do not participate.

    `frame_threshold_pct` drops an item whose in-window overlap is under that
    percentage of the shorter of its span and the window span, before the
    survivors are scored by overlap / item span. Containment in either
    direction always clears the threshold. Unlike the
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
    mirrors that verdict core so diagnostics use the same reasons.
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
    winner_indices = [None] * count
    verdicts = {}

    for item_index, item in enumerate(items):
        get = lambda key, default=None: _item_field(item, key, default)
        lane_index = _integer(get("lane_index", 0), 0)
        if lane_index < 0 or lane_index >= count or bool(get("muted", False)):
            verdicts[item_index] = REFERENCE_VERDICT["EXCLUDED"]
            continue
        if lane_index < len(configs):
            config = configs[lane_index]
            hidden = config.get("hidden", False) if isinstance(config, dict) else getattr(config, "hidden", False)
            if bool(hidden):
                verdicts[item_index] = REFERENCE_VERDICT["EXCLUDED"]
                continue
        item_start = min(max(0, _integer(get("start_frame", 0), 0)), max(0, duration - 1))
        raw_end = _integer(get("end_frame", -1), -1)
        item_end = duration if raw_end < 0 else min(duration, raw_end)
        if item_end <= item_start:
            verdicts[item_index] = REFERENCE_VERDICT["OUTSIDE"]
            continue
        overlap = max(0, min(item_end, end) - max(item_start, start))
        if overlap <= 0:
            verdicts[item_index] = REFERENCE_VERDICT["OUTSIDE"]
            continue
        specificity = overlap / (item_end - item_start)
        coverage = overlap / max(1, min(item_end - item_start, end - start))
        if threshold > 0 and coverage < threshold:
            verdicts[item_index] = REFERENCE_VERDICT["BELOW_THRESHOLD"]
            continue
        score = (specificity, item_start, item_index)
        if scores[lane_index] is None or score > scores[lane_index]:
            if winner_indices[lane_index] is not None:
                verdicts[winner_indices[lane_index]] = REFERENCE_VERDICT["SUPERSEDED"]
            winner_indices[lane_index] = item_index
            verdicts[item_index] = REFERENCE_VERDICT["WINNER"]
            scores[lane_index] = score
            winners[lane_index] = {"laneIndex": lane_index, "item": item}
        else:
            verdicts[item_index] = REFERENCE_VERDICT["SUPERSEDED"]
    return {"winners": winners, "verdicts": verdicts}


def resolve_effective_references(**kwargs) -> list:
    """Preserve the published winner shape while deriving it from verdicts."""
    return resolve_reference_verdicts(**kwargs)["winners"]


def resolve_reference_staging(**kwargs) -> dict:
    """Index all authored staging, including losers; absence identifies deletion."""
    resolved = resolve_reference_verdicts(**kwargs)
    items, members = {}, {}
    for index, item in enumerate(kwargs.get("reference_items") or []):
        item_id = str(_item_field(item, "reference_item_id", "") or "")
        if not item_id:
            continue
        items[item_id] = {
            "verdict": resolved["verdicts"][index],
            "lane_index": _integer(_item_field(item, "lane_index", 0)),
            "start_frame": _item_field(item, "start_frame", 0),
            "end_frame": _item_field(item, "end_frame", -1),
        }
        for binding in _item_field(item, "members", []) or []:
            member_id = str(_item_field(binding, "member_id", "") or "")
            if member_id:
                members.setdefault(member_id, []).append(item_id)
    return {"resolved": True, "items": items, "members": members}
