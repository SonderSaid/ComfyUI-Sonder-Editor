"""Prompt composition, window resolution, and PromptRelay payload building.

Single source of truth for how scene-global + segment-lane prompt state turns
into model-visible text. Imported by timeline_state.py (range selectors),
routes.py (prompt-payload route), and nodes/prompt_bridge.py (relay export).
The frontend compose-only mirror is web/js/prompt_composition.js — keep the
channel order, label constants, and join rules in lockstep with it.

Frame ranges are half-open [start, end) throughout, matching the editor.
"""

import re

from . import prompt_channel_templates as channel_templates

CHANNEL_ORDER = ("visual", "speech", "sounds")
DEFAULT_SECTION_DELIMITER = "."
CHANNEL_LABELS = {
    "visual": "[VISUAL]:",
    "speech": "[SPEECH]:",
    "sounds": "[SOUNDS]:",
}


def resolve_template(template=None) -> dict:
    """Normalize a template id / dict / None to a full template.

    None means the default `sonder` template, whose channels, labels and
    separators reproduce the pre-template three-channel composition exactly.
    """
    if template is None:
        return channel_templates.get_channel_template(
            channel_templates.DEFAULT_CHANNEL_TEMPLATE_ID)
    return channel_templates.get_channel_template(template)

# Matches PromptRelay's numeric weight tags exactly (parser.py _INLINE_TAG_RE):
# [12], [1.5], [0-50], [0:50]. Non-numeric brackets like [VISUAL] do NOT match.
_NUMERIC_TAG_RE = re.compile(r"\[([\d\.]+(?:[:\-][\d\.]+)?)\]")
_WHITESPACE_RE = re.compile(r"\s+")
_LINEBREAK_RE = re.compile(r"[\r\n]+")


def normalize_channels(raw=None, legacy_prompt="", keys=None) -> dict:
    """Return a channel dict of strings, filled for every key the caller names.

    `keys` is the template's ordered channel keys; None means the legacy
    {visual, speech, sounds} set, so un-updated callers keep today's shape.

    A present dict wins and **keys it carries that the template does not name
    are preserved**, not dropped. That preservation is the real backward- and
    forward-compatibility mechanism: filling defaults alone would turn every
    call site without template access into SILENT TRUNCATION rather than a
    loud failure. The sites that matter are `PromptSection.__init__` and its
    derived `.prompt` mirror, which have no project metadata to consult — and
    that mirror feeds prompt identity validation, so truncating there produces
    spurious `identity_mismatch` 409s on ordinary edits.

    Otherwise the legacy flat prompt string seeds the FIRST channel.
    """
    template_keys = tuple(keys) if keys is not None else CHANNEL_ORDER
    if isinstance(raw, dict):
        channels = {key: str(raw.get(key) or "") for key in template_keys}
        for key, value in raw.items():
            key = str(key)
            if key not in channels:
                channels[key] = str(value or "")
        return channels
    channels = {key: "" for key in template_keys}
    if template_keys:
        channels[template_keys[0]] = str(legacy_prompt or "")
    return channels


# Retention markers from the MiniMax full-reference guide (section 4.1). These
# are fixed English values in the output format, not free text.
SUBJECT_RETENTIONS = ("fully_preserved", "partially_preserved",
                      "attribute_transfer", "weak_reference")
DEFAULT_SUBJECT_RETENTION = "fully_preserved"


def normalize_subject_ids(raw) -> list:
    """Coerce a section's subject bindings to `[{entity_id, retention}, ...]`.

    The ONE normalizer used by the model, the routes and the identity check.
    Duplicates collapse (first wins) and authored order is preserved, because
    order is the tie-break when one section binds several subjects. A bare
    string is accepted as a bindng with the default retention. Unknown
    retention markers fall back to the default rather than reaching the model.
    """
    normalized = []
    seen = set()
    for entry in raw or []:
        if isinstance(entry, dict):
            entity_id = str(entry.get("entity_id") or "").strip()
            retention = str(entry.get("retention") or "").strip()
        else:
            entity_id = str(entry or "").strip()
            retention = ""
        if not entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        normalized.append({
            "entity_id": entity_id,
            "retention": retention if retention in SUBJECT_RETENTIONS
            else DEFAULT_SUBJECT_RETENTION,
        })
    return normalized


def normalize_channel_exceptions(raw) -> list:
    """Coerce a section's global-channel opt-outs to a sorted key list.

    Sorted rather than authored-order: this is a SET of keys, so two sections
    that opted out of the same channels must compare equal and produce the same
    bytes in project.json regardless of the order the checkboxes were clicked.
    """
    keys = set()
    for entry in raw or []:
        key = str(entry or "").strip()
        if key:
            keys.add(key)
    return sorted(keys)


def section_inherits_global(section, key) -> bool:
    """Whether one section inherits the scene-global text for `key`.

    Default ON: absence from the exceptions set means inherit, which is what
    keeps a channel added to the template later inherited everywhere without
    rewriting a single section.
    """
    if isinstance(section, dict):
        raw = section.get("global_channel_exceptions")
    else:
        raw = getattr(section, "global_channel_exceptions", None)
    return str(key) not in set(raw or ())


def subject_ids_identity(raw) -> list:
    """Order-insensitive projection of subject bindings, for 409 comparison.

    Storage keeps authored order (it is the within-section numbering tie-break)
    but a client that merely reordered or duplicated bindings has not changed
    the section, so identity validation must not reject it.
    """
    return sorted(normalize_subject_ids(raw), key=lambda entry: entry["entity_id"])


def merge_channels(existing, incoming) -> dict:
    """Apply an incoming channel patch over a section's stored channels.

    A client only ever sends the channels of the template it is authoring
    under, so REPLACING would delete any channel belonging to another template
    — making a template switch away and back into silent data loss. Keys that
    are sent win, including when they are sent empty (that is how a channel is
    cleared); keys that are not sent are left alone.
    """
    merged = dict(existing) if isinstance(existing, dict) else {}
    if isinstance(incoming, dict):
        for key, value in incoming.items():
            merged[str(key)] = str(value or "")
    return normalize_channels(merged)


_CHANNEL_HEADER_RE = re.compile(r"^([A-Za-z0-9_]+):[ \t]?(.*)$")


def join_channel_headers(channels, template=None) -> str:
    """Flatten a channel dict to one text with `key:` headers.

    Template channels come first in template order; any other non-empty key
    follows, so text belonging to a third template is carried rather than
    dropped when switching A to B to C.

    A template holding exactly ONE channel emits no header at all. Headers
    exist to tell channels apart, and there is nothing to tell apart — but more
    importantly, that channel's text is itself a collapsed document whenever it
    came from a previous narrowing, and re-labelling it would nest one document
    inside another and strand a bare `visual:` line on the way back out.
    """
    resolved = resolve_template(template)
    keys = list(channel_templates.template_channel_keys(resolved))
    source = channels if isinstance(channels, dict) else {}
    for key in source:
        if key not in keys:
            keys.append(str(key))
    populated = [(key, str(source.get(key) or "").strip()) for key in keys]
    populated = [(key, text) for key, text in populated if text]
    if len(populated) == 1 and len(channel_templates.template_channel_keys(resolved)) == 1:
        return populated[0][1]
    return "\n\n".join(f"{key}:\n{text}" for key, text in populated)


def split_channel_headers(text, template=None) -> dict:
    """Parse `key:` headers back into a channel dict for `template`.

    The parse is ANCHORED: a header is a line that starts with a key belonging
    to this template, followed by a colon. Nothing else counts — a MiniMax
    description legitimately contains `says: <d>[English] ...</d>`, and a
    looser "word followed by a colon" rule would shred it.

    Text before the first recognised header, and any header this template does
    not know, stays in the FIRST channel with its label intact — visible and
    movable by hand rather than silently discarded.
    """
    resolved = resolve_template(template)
    keys = list(channel_templates.template_channel_keys(resolved))
    if not keys:
        return {}
    known = set(keys)
    buffers = {key: [] for key in keys}
    current = keys[0]
    for line in str(text or "").split("\n"):
        match = _CHANNEL_HEADER_RE.match(line)
        if match and match.group(1) in known:
            current = match.group(1)
            if match.group(2):
                buffers[current].append(match.group(2))
            continue
        buffers[current].append(line)
    return {key: "\n".join(lines).strip() for key, lines in buffers.items()}


def collapse_channels_for_template(channels, from_template=None, to_template=None) -> dict:
    """The channel patch for moving one section between templates.

    Collapse against the OUTGOING template, re-split against the INCOMING one.
    One rule serves both directions: narrowing collapses stranded text under
    its own `key:` header, and widening re-splits it into real channels when
    the headers match. Either way the text stays visible instead of surviving
    as unreachable keys.

    Every outgoing key is included as an explicit empty string, because channel
    updates MERGE — a key simply left out would keep its old value and the
    collapsed copy would appear twice.
    """
    collapsed = join_channel_headers(channels, from_template)
    patch = {}
    source = channels if isinstance(channels, dict) else {}
    for key in channel_templates.template_channel_keys(resolve_template(from_template)):
        patch[key] = ""
    for key in source:
        patch[str(key)] = ""
    patch.update(split_channel_headers(collapsed, to_template))
    return patch


def compose_section_text(channels, labels_on=True, template=None) -> str:
    """Compose one section's channels into a single string.

    Channels are emitted in template order and empty channels are omitted.
    With labels on, each part is `label + label_separator + text`; parts are
    joined by the template's field separator. The default template reproduces
    `[VISUAL]: a shot [SPEECH]: hello` exactly.
    """
    if not isinstance(channels, dict):
        return ""
    resolved = resolve_template(template)
    label_separator = resolved.get("label_separator", " ")
    parts = []
    for channel in resolved.get("channels") or ():
        text = str(channels.get(channel["key"]) or "").strip()
        if not text:
            continue
        if labels_on and channel.get("label"):
            parts.append(f"{channel['label']}{label_separator}{text}")
        else:
            parts.append(text)
    return str(resolved.get("field_separator", " ")).join(parts)


def compose_window_prompt(global_text, channels_or_none, labels_on=True,
                          template=None) -> str:
    """Global text + one section's composed text (either part may be empty)."""
    parts = []
    global_part = str(global_text or "").strip()
    if global_part:
        parts.append(global_part)
    if channels_or_none is not None:
        section_part = compose_section_text(channels_or_none, labels_on, template)
        if section_part:
            parts.append(section_part)
    return " ".join(parts)


def _section_entries(sections, keys=None):
    """Normalize PromptSection objects OR raw snapshot dicts to plain entries.

    Raw dicts may be pre-upgrade v1 queue snapshots carrying only a flat
    `prompt` — the legacy→first-channel fallback applies (audit F15).
    """
    entries = []
    for section in sections or []:
        if isinstance(section, dict):
            if section.get("muted"):
                continue
            try:
                start = int(section.get("start_frame", 0))
                end = int(section.get("end_frame", 0))
            except (TypeError, ValueError):
                continue
            channels = normalize_channels(
                section.get("channels"), legacy_prompt=section.get("prompt", ""),
                keys=keys,
            )
            starts_new_shot = bool(section.get("starts_new_shot", False))
            shot_timestamp = bool(section.get("shot_timestamp", False))
            subject_ids = normalize_subject_ids(section.get("subject_ids"))
            exceptions = normalize_channel_exceptions(
                section.get("global_channel_exceptions"))
        else:
            try:
                start = int(getattr(section, "start_frame", 0))
                end = int(getattr(section, "end_frame", 0))
            except (TypeError, ValueError):
                continue
            if getattr(section, "muted", False):
                continue
            channels = normalize_channels(getattr(section, "channels", None),
                                          legacy_prompt=getattr(section, "prompt", ""),
                                          keys=keys)
            starts_new_shot = bool(getattr(section, "starts_new_shot", False))
            shot_timestamp = bool(getattr(section, "shot_timestamp", False))
            subject_ids = normalize_subject_ids(getattr(section, "subject_ids", None))
            exceptions = normalize_channel_exceptions(
                getattr(section, "global_channel_exceptions", None))
        entries.append({"start": start, "end": end, "channels": channels,
                        "starts_new_shot": starts_new_shot,
                        "shot_timestamp": shot_timestamp,
                        "subject_ids": subject_ids,
                        "global_channel_exceptions": exceptions})
    return entries


def resolve_segments(sections, window_start, window_end, labels_on=True,
                     boundary_threshold_pct=0.0, template=None):
    """Resolve the segment lane into contiguous window-local segments.

    Four passes:
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
    4. Boundary-spill threshold (only when boundary_threshold_pct > 0): only
       the FIRST and LAST segments can be clipped by the window edge. Drop a
       window-clipped end segment whose in-window coverage is below
       boundary_threshold_pct% of its source section's AUTHORED length, and
       let the neighbor absorb the freed span (hold-until-next). Never reduce
       below one surviving segment, so a window sitting inside a single long
       section is never emptied. Stops frame-constraint snapping from
       bleeding a few frames of an adjacent section into the generation.

    Each returned segment additionally carries `section_start` (the source
    section's authored start_frame) so callers can map a window-local segment
    back to its originating section. Sections whose channels compose to "" are
    dropped before coverage so an empty section never claims a span. Zero
    sections → [].
    """
    if window_end <= window_start:
        return []
    resolved_template = resolve_template(template)
    keys = channel_templates.template_channel_keys(resolved_template)
    composed = []
    for entry in _section_entries(sections, keys=keys):
        if entry["end"] <= entry["start"]:
            continue
        text = compose_section_text(entry["channels"], labels_on, resolved_template)
        if not text:
            continue
        composed.append({**entry, "text": text})
    composed.sort(key=lambda e: e["start"])

    # Pass 1 — first-wins overlap normalization. Carry the authored start/end
    # (pre-overlap-push) so Pass 4 can measure window clipping against each
    # section's own authored length and map segments back to source sections.
    survivors = []
    cursor = None
    for entry in composed:
        eff_start = entry["start"] if cursor is None else max(entry["start"], cursor)
        if eff_start >= entry["end"]:
            continue  # fully shadowed by an earlier section
        survivors.append({"start": eff_start, "text": entry["text"],
                          "channels": entry["channels"],
                          "starts_new_shot": entry.get("starts_new_shot", False),
                          "shot_timestamp": entry.get("shot_timestamp", False),
                          "subject_ids": entry.get("subject_ids", []),
                          "global_channel_exceptions":
                              entry.get("global_channel_exceptions", []),
                          "authored_start": entry["start"],
                          "authored_end": entry["end"]})
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
            "starts_new_shot": entry.get("starts_new_shot", False),
            "shot_timestamp": entry.get("shot_timestamp", False),
            "subject_ids": entry.get("subject_ids", []),
            "global_channel_exceptions": entry.get("global_channel_exceptions", []),
            "section_start": entry["authored_start"],
            "authored_start": entry["authored_start"],
            "authored_end": entry["authored_end"],
            "start": cov_start - window_start,
            "end": cov_end - window_start,
        })

    # Pass 4 — boundary-spill threshold. Only the first and last segments can
    # be window-clipped (middle segments are bounded by neighbors). Drop a
    # clipped end whose in-window coverage is a small fraction of its source
    # section's authored length; the neighbor absorbs the freed span. The
    # len>=2 guards keep at least one surviving segment so the window is never
    # emptied (e.g. a selection entirely inside one long section).
    if boundary_threshold_pct > 0 and len(segments) >= 2:
        frac = boundary_threshold_pct / 100.0
        last = segments[-1]
        last_authored_len = max(1, last["authored_end"] - last["authored_start"])
        if (last["authored_end"] > window_end
                and (last["end"] - last["start"]) < frac * last_authored_len):
            freed_end = last["end"]
            segments.pop()
            segments[-1]["end"] = freed_end
        if len(segments) >= 2:
            first = segments[0]
            first_authored_len = max(1, first["authored_end"] - first["authored_start"])
            if (first["authored_start"] < window_start
                    and (first["end"] - first["start"]) < frac * first_authored_len):
                segments.pop(0)
                segments[0]["start"] = 0

    for seg in segments:
        seg.pop("authored_start", None)
        seg.pop("authored_end", None)
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


def _shot_marked_texts(segments, key, fps) -> list:
    """Per-segment texts for the shot-marker channel, with markers inserted.

    Opening a shot and stamping a cut time are FULLY independent choices, and a
    section may do either, both or neither:

        starts_new_shot  shot_timestamp   prefix
        off              off              (none)
        on               off              `[Shot N] `
        off              on               `At MM:SS.mmm, `
        on               on               `[Shot N] At MM:SS.mmm, `

    Nothing is forced on the first segment: if no section opens a shot, no
    `[Shot N]` is emitted at all, and a first section that explicitly asks for a
    cut time gets `At 00:00.000,`. Numbering is dense over the EFFECTIVE set and
    starts at whichever segment first opens a shot.

    Timestamps are window-local, so the same section reads correctly whether the
    whole scene or one slice of it is rendered.

    Markers are interleaved into ONE channel's join because a labels-on compose
    groups every segment of a channel under a single label, so a marker cannot
    prefix the whole output.
    """
    texts = []
    shot_number = 0
    for segment in segments or []:
        text = str((segment.get("channels") or {}).get(key) or "").strip()
        marker = ""
        if bool(segment.get("starts_new_shot")):
            shot_number += 1
            marker = f"[Shot {shot_number}]"
        if bool(segment.get("shot_timestamp")):
            timecode = channel_templates.format_shot_timecode(
                segment.get("start", 0), fps)
            if timecode:
                marker = f"{marker} At {timecode},".strip()
        if marker:
            text = f"{marker} {text}".strip() if text else marker
        if text:
            texts.append(text)
    return texts


def compose_range_prompt(global_text, sections, window_start, window_end,
                         labels_on=True, delimiter=DEFAULT_SECTION_DELIMITER,
                         boundary_threshold_pct=0.0, template=None,
                         fps=0.0, global_channels=None) -> str:
    """THE single-string composer: global + ALL window segments, joined by the
    section-seam delimiter. Used by the Scene selectors, the queue handlers'
    frozen-prompt compose, and the dormant summary.

    Labels ON groups by channel — each label appears ONCE with every
    segment's text for that channel joined in temporal order
    (`[VISUAL]: a dog. it barks [SPEECH]: …`), never repeated per segment.
    Labels OFF keeps plain temporal concatenation of per-segment text.
    `boundary_threshold_pct` forwards to resolve_segments (boundary-spill
    drop). Relay payloads are NOT affected — each relay segment must stay
    self-contained, so the bridge keeps per-segment labels.

    `template` selects the channel set, labels, and the separators between
    fields; None is the default three-channel behavior, byte for byte. The
    template OWNS the label policy, because `labels_on` defaults to False at
    every read site and a named-field format with no field names is prose soup.

    `fps` is consumed by shot-marker timestamps and is inert for a template
    that declares no shot-marker channel.
    """
    resolved = resolve_template(template)
    effective_labels = channel_templates.template_labels_on(resolved, labels_on)
    segments = resolve_segments(sections, window_start, window_end,
                                effective_labels, boundary_threshold_pct, resolved)
    global_part = str(global_text or "").strip()
    # A template with global channels OFF has one global box, so there is no
    # channel to merge into and the text leads the whole payload.
    per_channel_global = (
        channel_templates.effective_global_merge(resolved)
        == channel_templates.GLOBAL_MERGE_PER_CHANNEL
    )

    shot_channel = resolved.get("shot_marker_channel") or ""
    # The key a single, un-channelled global text opts out under.
    global_keys = channel_templates.global_channel_keys(resolved)
    lead_key = global_keys[0] if global_keys else ""
    if global_channels is not None and not per_channel_global:
        # `global_text` is the LEGACY three-channel mirror, which is empty for
        # any template whose channels are named something else — so a leading
        # merge would silently drop the whole global prompt on, say, MiniMax
        # with per-channel globals turned off. Derive it from the channels the
        # global prompt actually authors under instead.
        derived = " ".join(
            text for text in
            (str((global_channels or {}).get(key) or "").strip() for key in global_keys)
            if text)
        if derived:
            global_part = derived

    def _any_segment_inherits(key):
        """Emit-once-if-any: a global channel appears at the head of its own
        channel exactly once, provided at least one segment the window actually
        reaches still inherits it.

        Repeating it per inheriting section would print a style opening several
        times over; emitting it regardless would make the per-section checkbox
        do nothing on a full-scene render. This reading keeps it meaningful
        exactly where it is used — rendering one section at a time, where
        opting out drops the global text entirely.
        """
        if not segments:
            return True
        return any(section_inherits_global(segment, key) for segment in segments)

    def _channel_texts(key):
        if key and key == shot_channel:
            return _shot_marked_texts(segments, key, fps)
        return [text for text in
                (str((s.get("channels") or {}).get(key) or "").strip() for s in segments)
                if text]

    if effective_labels:
        label_separator = str(resolved.get("label_separator", " "))
        parts = []
        for channel in resolved.get("channels") or ():
            key = channel["key"]
            texts = _channel_texts(key)
            if per_channel_global:
                # Each global channel merges at the head of ITS OWN channel's
                # temporal join. That is what puts a MiniMax style opening
                # ahead of [Shot 1] inside detailed_description, where its
                # guide wants it, instead of before the first field name.
                lead = str((global_channels or {}).get(key) or "").strip()
                if lead and not _any_segment_inherits(key):
                    lead = ""
                if lead:
                    texts = [lead] + texts
                elif (global_part and not global_channels and not parts
                        and _any_segment_inherits(key)):
                    # No per-channel global authored: fall back to the flat
                    # global text at the head of the first emitted field, which
                    # is still inside a field rather than ahead of one.
                    texts = [global_part] + texts
            if not texts:
                continue
            body = join_segment_texts(texts, delimiter)
            label = channel.get("label")
            parts.append(f"{label}{label_separator}{body}" if label else body)
        section_text = str(resolved.get("field_separator", " ")).join(parts)
    else:
        texts = [s["text"] for s in segments]
        if per_channel_global and global_part and _any_segment_inherits(lead_key):
            texts = [global_part] + texts
        section_text = join_segment_texts(texts, delimiter)

    if per_channel_global:
        # Already merged above — unless nothing composed, in which case the
        # global text is the whole payload rather than being dropped.
        return section_text or global_part
    # Leading merge: ONE global text ahead of everything, so the opt-out that
    # matters is the one on the template's first (and only) global channel.
    if global_part and not _any_segment_inherits(lead_key):
        global_part = ""
    return " ".join(part for part in (global_part, section_text) if part)


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
