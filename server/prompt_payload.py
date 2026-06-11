"""Prompt composition, window resolution, and PromptRelay payload building.

Single source of truth for how scene-global + segment-lane prompt state turns
into model-visible text. Imported by timeline_state.py (range selectors),
routes.py (prompt-payload route), and nodes/prompt_bridge.py (relay export).
The frontend compose-only mirror is web/js/prompt_composition.js — keep the
channel order, label constants, and join rules in lockstep with it.

Frame ranges are half-open [start, end) throughout, matching the editor.
"""

import re

CHANNEL_ORDER = ("visual", "speech", "sounds")
DEFAULT_SECTION_DELIMITER = "."
CHANNEL_LABELS = {
    "visual": "[VISUAL]:",
    "speech": "[SPEECH]:",
    "sounds": "[SOUNDS]:",
}

# Matches PromptRelay's numeric weight tags exactly (parser.py _INLINE_TAG_RE):
# [12], [1.5], [0-50], [0:50]. Non-numeric brackets like [VISUAL] do NOT match.
_NUMERIC_TAG_RE = re.compile(r"\[([\d\.]+(?:[:\-][\d\.]+)?)\]")
_WHITESPACE_RE = re.compile(r"\s+")
_LINEBREAK_RE = re.compile(r"[\r\n]+")


def normalize_channels(raw=None, legacy_prompt="") -> dict:
    """Return a full {visual, speech, sounds} dict of strings.

    A present dict wins (missing keys filled with ""); otherwise the legacy
    flat prompt string seeds the visual channel.
    """
    if isinstance(raw, dict):
        return {key: str(raw.get(key) or "") for key in CHANNEL_ORDER}
    channels = {key: "" for key in CHANNEL_ORDER}
    channels["visual"] = str(legacy_prompt or "")
    return channels


def compose_section_text(channels, labels_on=True) -> str:
    """Compose one section's channels into a single string.

    Channel order is visual, speech, sounds; empty channels are omitted.
    With labels on, each part is prefixed `[VISUAL]: ...` etc.
    """
    if not isinstance(channels, dict):
        return ""
    parts = []
    for key in CHANNEL_ORDER:
        text = str(channels.get(key) or "").strip()
        if not text:
            continue
        parts.append(f"{CHANNEL_LABELS[key]} {text}" if labels_on else text)
    return " ".join(parts)


def compose_window_prompt(global_text, channels_or_none, labels_on=True) -> str:
    """Global text + one section's composed text (either part may be empty)."""
    parts = []
    global_part = str(global_text or "").strip()
    if global_part:
        parts.append(global_part)
    if channels_or_none is not None:
        section_part = compose_section_text(channels_or_none, labels_on)
        if section_part:
            parts.append(section_part)
    return " ".join(parts)


def _section_entries(sections):
    """Normalize PromptSection objects OR raw snapshot dicts to plain entries.

    Raw dicts may be pre-upgrade v1 queue snapshots carrying only a flat
    `prompt` — the legacy→visual fallback applies (audit F15).
    """
    entries = []
    for section in sections or []:
        if isinstance(section, dict):
            try:
                start = int(section.get("start_frame", 0))
                end = int(section.get("end_frame", 0))
            except (TypeError, ValueError):
                continue
            channels = normalize_channels(
                section.get("channels"), legacy_prompt=section.get("prompt", "")
            )
        else:
            try:
                start = int(getattr(section, "start_frame", 0))
                end = int(getattr(section, "end_frame", 0))
            except (TypeError, ValueError):
                continue
            channels = normalize_channels(getattr(section, "channels", None),
                                          legacy_prompt=getattr(section, "prompt", ""))
        entries.append({"start": start, "end": end, "channels": channels})
    return entries


def resolve_segments(sections, window_start, window_end, labels_on=True):
    """Resolve the segment lane into contiguous window-local segments.

    Three passes:
    1. First-wins normalization of (legacy) overlaps: sections sorted by
       start; a later section's start is pushed past the previous section's
       end; fully shadowed sections drop. The EARLIER section keeps the
       overlap zone, matching the legacy first-match selector.
    2. Hold-until-next coverage at scene level: each surviving section covers
       from its effective start until the next section's start; the last
       holds forever; the first also covers everything before it. This is the
       approved gap rule and is what keeps PromptRelay's proportional length
       normalization honest — gaps would silently distort segment timing.
    3. Clip coverage to [window_start, window_end), drop empty spans, rebase
       to window-local 0-based frames.

    Sections whose channels compose to "" are dropped before coverage so an
    empty section never claims a span. Zero sections → [].
    """
    if window_end <= window_start:
        return []
    composed = []
    for entry in _section_entries(sections):
        if entry["end"] <= entry["start"]:
            continue
        text = compose_section_text(entry["channels"], labels_on)
        if not text:
            continue
        composed.append({**entry, "text": text})
    composed.sort(key=lambda e: e["start"])

    # Pass 1 — first-wins overlap normalization.
    survivors = []
    cursor = None
    for entry in composed:
        eff_start = entry["start"] if cursor is None else max(entry["start"], cursor)
        if eff_start >= entry["end"]:
            continue  # fully shadowed by an earlier section
        survivors.append({"start": eff_start, "text": entry["text"],
                          "channels": entry["channels"]})
        cursor = entry["end"]

    if not survivors:
        return []

    # Pass 2 + 3 — hold-until-next coverage, clipped to the window, rebased.
    segments = []
    for i, entry in enumerate(survivors):
        cov_start = entry["start"] if i > 0 else window_start
        cov_start = max(cov_start, window_start)
        cov_end = survivors[i + 1]["start"] if i + 1 < len(survivors) else window_end
        cov_end = min(cov_end, window_end)
        if cov_end <= cov_start:
            continue
        segments.append({
            "text": entry["text"],
            "channels": entry["channels"],
            "start": cov_start - window_start,
            "end": cov_end - window_start,
        })
    return segments


def join_segment_texts(texts, delimiter=DEFAULT_SECTION_DELIMITER) -> str:
    """Join segment texts with the section-seam delimiter.

    Non-empty trimmed delimiter joins as `text<delim> text`; empty delimiter
    falls back to a plain space join. No trailing-punctuation dedup (v1
    simplicity, documented).
    """
    parts = [str(t or "").strip() for t in (texts or [])]
    parts = [p for p in parts if p]
    seam = str(delimiter or "").strip()
    joiner = f"{seam} " if seam else " "
    return joiner.join(parts)


def compose_range_prompt(global_text, sections, window_start, window_end,
                         labels_on=True, delimiter=DEFAULT_SECTION_DELIMITER) -> str:
    """THE single-string composer: global + ALL window segments, joined by the
    section-seam delimiter. Used by the Scene selectors, the queue handlers'
    frozen-prompt compose, and the dormant summary.

    Labels ON groups by channel — each label appears ONCE with every
    segment's text for that channel joined in temporal order
    (`[VISUAL]: a dog. it barks [SPEECH]: …`), never repeated per segment.
    Labels OFF keeps plain temporal concatenation of per-segment text.
    Relay payloads are NOT affected — each relay segment must stay
    self-contained, so the bridge keeps per-segment labels.
    """
    segments = resolve_segments(sections, window_start, window_end, labels_on)
    if labels_on:
        parts = []
        for key in CHANNEL_ORDER:
            texts = [str((s.get("channels") or {}).get(key) or "").strip() for s in segments]
            texts = [t for t in texts if t]
            if texts:
                parts.append(f"{CHANNEL_LABELS[key]} {join_segment_texts(texts, delimiter)}")
        section_text = " ".join(parts)
    else:
        section_text = join_segment_texts([s["text"] for s in segments], delimiter)
    parts = [part for part in (str(global_text or "").strip(), section_text) if part]
    return " ".join(parts)


def sanitize_segment_text(text) -> str:
    """Make one local segment's text safe for the PromptRelay parsers.

    1. `|` → `,` — the parsers split locals on every pipe with no escape.
    2. Newlines → spaces — any line ending `<words> <number>:` flips the
       Smart parser into block mode for the WHOLE payload.
    3. Numeric bracket tags `[n]` / `[n-m]` / `[n:m]` → inner text — the
       first numeric tag in a segment would hijack its weight.
    Global text is exempt by design (never pipe-split or tag-parsed).
    """
    cleaned = str(text or "").replace("|", ",")
    cleaned = _LINEBREAK_RE.sub(" ", cleaned)
    cleaned = _NUMERIC_TAG_RE.sub(r"\1", cleaned)
    return _WHITESPACE_RE.sub(" ", cleaned).strip()


def build_relay_payload(global_text, segments) -> dict:
    """Build the four PromptRelay strings from window-local segments.

    Segments whose text sanitizes to empty are dropped and the span is
    absorbed by the previous neighbor (hold-until-next), keeping
    `segment_lengths` aligned with the surviving pipe-joined locals —
    PromptRelay drops empty locals after pipe-split WITHOUT re-aligning
    lengths, which would shift every later segment's timing (audit F10).
    """
    sanitized = []
    for segment in segments or []:
        text = sanitize_segment_text(segment.get("text", ""))
        if not text:
            continue
        sanitized.append({"text": text, "start": int(segment["start"]),
                          "end": int(segment["end"])})

    if sanitized and segments:
        total_start = int(segments[0]["start"])
        total_end = int(segments[-1]["end"])
        sanitized[0]["start"] = total_start
        for i in range(len(sanitized) - 1):
            sanitized[i]["end"] = sanitized[i + 1]["start"]
        sanitized[-1]["end"] = total_end

    smart_parts = [f"{s['text']} [{s['start']}-{s['end']}]" for s in sanitized]
    local_parts = [s["text"] for s in sanitized]
    lengths = [str(s["end"] - s["start"]) for s in sanitized]
    return {
        "global_prompt": str(global_text or "").strip(),
        "smart_prompt": " | ".join(smart_parts),
        "local_prompts": " | ".join(local_parts),
        "segment_lengths": ",".join(lengths),
        "segments": sanitized,
    }
