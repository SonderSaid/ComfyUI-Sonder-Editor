"""Prompt channel templates — the declaration of what a channel set *is*.

A template names the ordered set of prompt channels a project authors, how each
one is labelled, what separates one field from the next, and which channel (if
any) carries shot markers. `server/prompt_payload.py` stays authoritative for
composition; this module is authoritative for the *format vocabulary* that
composition is given.

Shipped presets are backend/frontend mirrors and are read-only. Browser settings
own a cross-project catalog of custom templates and the default for newly created
projects. The active project stores a thin preset id or a full custom dict, while
every queued job freezes the resolved full dict. Browser catalog changes never
rewrite projects implicitly; composition uses only the value the project or job
already carries.

The frontend mirror is web/js/prompt_channel_templates.js — keep the preset ids,
channel keys, labels and separators in lockstep with it.

Emission is `label + label_separator + text` per channel, with `field_separator`
between channels. The default `sonder` template reproduces the pre-template
three-channel behavior exactly: `[VISUAL]: text [SPEECH]: text`.

Format authority for the MiniMax presets is the MiniMax H3 prompt-writing
guides (`VIDEO_PROMPT_WRITING_GUIDE_base_en.md` and `..._ref_en.md` in
MiniMaxAI/MiniMax-H3). Where this file and those guides disagree, the guides win.
"""

DEFAULT_CHANNEL_TEMPLATE_ID = "sonder"
# Creation default is deliberately separate from the legacy/composition
# fallback above: changing the latter would reinterpret old projects.
DEFAULT_NEW_PROJECT_CHANNEL_TEMPLATE_ID = "standard"

# Label policy. New templates own their label behavior. LABELS_PROJECT remains
# only for legacy custom definitions/jobs created while the removed project
# toggle still existed.
LABELS_ALWAYS = "always"      # template forces labels on
LABELS_NEVER = "never"        # template forces labels off
LABELS_PROJECT = "project"    # honor a legacy job's frozen prompt_channel_labels value

# Global-prompt merge policy (P5). `leading` prepends the global text once,
# ahead of everything — today's behavior. `per_channel` merges each global
# channel at the head of its own channel's temporal join.
GLOBAL_MERGE_LEADING = "leading"
GLOBAL_MERGE_PER_CHANNEL = "per_channel"


def global_channels_enabled(template) -> bool:
    """Whether the scene-global prompt is authored per channel.

    Off means ONE global box for the whole scene. Its text is emitted ahead of
    everything, so a template that would otherwise merge per channel falls back
    to `leading` — see `effective_global_merge`.
    """
    resolved = template if isinstance(template, dict) else get_channel_template(template)
    return resolved.get("global_channels_enabled", True) is not False


def effective_global_merge(template) -> str:
    """The merge policy a composition actually uses.

    A single global box cannot merge per channel — there is no channel to merge
    it into — so disabling global channels forces `leading` regardless of what
    the template declares.
    """
    resolved = template if isinstance(template, dict) else get_channel_template(template)
    if not global_channels_enabled(resolved):
        return GLOBAL_MERGE_LEADING
    merge = resolved.get("global_merge", GLOBAL_MERGE_LEADING)
    return merge if merge in (GLOBAL_MERGE_LEADING, GLOBAL_MERGE_PER_CHANNEL) else GLOBAL_MERGE_LEADING


def global_channel_keys(template) -> tuple:
    """The channel keys the scene-global prompt authors under this template.

    Every channel when global channels are on; the first channel alone when they
    are off, so the single box still has a durable place to live and switching
    the flag is a collapse/re-split rather than a data loss.
    """
    resolved = template if isinstance(template, dict) else get_channel_template(template)
    keys = template_channel_keys(resolved)
    if not keys:
        return keys
    return keys if global_channels_enabled(resolved) else keys[:1]


def default_draft_channel(template) -> str:
    """The channel unheadered draft text belongs to under this template.

    A Writing draft is one flat box carrying every channel behind `key:`
    headers, so text above the first header has no channel of its own. It used
    to land in channel 1 by default, which is right for `standard` and `sonder`
    and wrong for MiniMax H3 Full Reference, where channel 1 is
    `subject_definitions` and narrative prose became subject definitions.

    `""` means UNDECLARED, not "no channel" — unlike `shot_marker_channel`,
    which legitimately points nowhere. Unheadered text must always land
    somewhere, so the empty value resolves to the first channel: that is the
    behavior every template shipped with, and it stays the answer for a template
    whose author never thought about this. Only a template that names a channel
    moves it. An unresolvable name is treated as undeclared for the same reason
    the marker pointer is blanked — a dangling key must not strand text.

    This is the ONE place `""` is interpreted; no call site may re-derive it.
    """
    resolved = template if isinstance(template, dict) else get_channel_template(template)
    keys = template_channel_keys(resolved)
    if not keys:
        return ""
    declared = str(resolved.get("default_draft_channel") or "")
    return declared if declared in keys else keys[0]


def _channel(key, label, description):
    return {"key": key, "label": label, "description": description}


_SONDER_CHANNELS = (
    _channel("visual", "[VISUAL]:",
             "What is seen: subjects, action, framing, style and lighting."),
    _channel("speech", "[SPEECH]:",
             "What is said or sung, including who says it."),
    _channel("sounds", "[SOUNDS]:",
             "What is heard that is not speech: ambience, effects, music."),
)

# --- MiniMax H3 ---------------------------------------------------------------
# Shared between both MiniMax presets (base guide 4.6 / 4.7, ref guide 6).
_MINIMAX_SOUNDSCAPE = _channel(
    "overall_soundscape", "overall_soundscape",
    "1-4 English sentences summarizing ambience, physical action sounds and "
    "non-verbal human sounds across the WHOLE video. Dialogue, singing and "
    "diegetic music belong in the description field instead.")
_MINIMAX_NON_DIEGETIC = _channel(
    "non_diegetic_music", "non_diegetic_music",
    "1-3 English sentences describing background music only the audience "
    "hears. Give instrumentation, tempo, rhythm and dynamics — not mood words. "
    "Music the characters can hear is diegetic and belongs in the description.")

_MINIMAX_BASE_CHANNELS = (
    _channel("integrated_multimodal_description", "integrated_multimodal_description",
             "The main body. Visuals, actions, shots, speakers, dialogue and "
             "diegetic audio along the timeline. Open with the overall style "
             "and initial composition. Shot markers are inserted here."),
    _MINIMAX_SOUNDSCAPE,
    _MINIMAX_NON_DIEGETIC,
)

_MINIMAX_REF_CHANNELS = (
    _channel("subject_definitions", "subject_definitions",
             "One `<Subject N> is ...` line per piece of referenced content, "
             "saying what the label denotes and the features to follow. Also "
             "define `<Picture N>`, `<Video N>` and `<Audio N>` here when they "
             "are tracked separately."),
    _channel("summary", "summary",
             "One short paragraph summarizing the target video and its "
             "reference relationships, opening with a bracketed task-type "
             "prefix such as `[reference generation + audio reference]`. Use "
             "the reference labels; do not introduce new ones."),
    _channel("retention_analysis", "retention_analysis",
             "One line per reference label saying how it is preserved, using "
             "the fixed markers fully_preserved, partially_preserved, "
             "attribute_transfer or weak_reference (audio uses fully_copy, "
             "partially_copy, reference, weak_reference)."),
    _channel("detailed_description", "detailed_description",
             "The main body. Visuals, actions, sound and dialogue shot by shot "
             "in playback order, inserting `<Subject N>`, `<Picture N>`, "
             "`<Video N>` and `<Audio N>` where they apply. Shot markers are "
             "inserted here."),
    _MINIMAX_SOUNDSCAPE,
    _MINIMAX_NON_DIEGETIC,
)


def _template(template_id, name, description, channels, *, field_separator=" ",
              label_separator=" ", labels=LABELS_PROJECT, shot_marker_channel="",
              default_draft_channel="", global_merge=GLOBAL_MERGE_LEADING,
              global_channels_on=True, default_context_profile="generic@1"):
    return {
        "id": template_id,
        "name": name,
        "description": description,
        "channels": tuple(channels),
        "field_separator": field_separator,
        "label_separator": label_separator,
        "labels": labels,
        "shot_marker_channel": shot_marker_channel,
        "default_draft_channel": default_draft_channel,
        "global_merge": global_merge,
        "global_channels_enabled": global_channels_on,
        "default_context_profile": default_context_profile,
        "builtin": True,
    }


PROMPT_CHANNEL_TEMPLATE_PRESETS = {
    "standard": _template(
        "standard", "Standard",
        "One plain prompt channel, with no field names.",
        (_channel("visual", "",
                  "The whole prompt for this section."),),
        labels=LABELS_NEVER,
        shot_marker_channel="visual",
    ),
    DEFAULT_CHANNEL_TEMPLATE_ID: _template(
        # Display name only — the id stays `sonder` so existing projects and
        # frozen jobs keep resolving. This channel split is not ours to claim.
        DEFAULT_CHANNEL_TEMPLATE_ID, "Visual + Speech + Sound",
        "The editor's three-field format: separate visual, speech and sound channels, "
        "always labelled in composed prompt output.",
        _SONDER_CHANNELS,
        labels=LABELS_ALWAYS, shot_marker_channel="visual",
    ),
    "minimax_h3_base": _template(
        "minimax_h3_base", "MiniMax H3",
        "MiniMax H3's three core fields for text- and keyframe-driven "
        "generation.",
        _MINIMAX_BASE_CHANNELS,
        field_separator="\n\n", label_separator=": ", labels=LABELS_ALWAYS,
        shot_marker_channel="integrated_multimodal_description",
        default_draft_channel="integrated_multimodal_description",
        global_merge=GLOBAL_MERGE_PER_CHANNEL,
        default_context_profile="minimax_h3_base@1",
    ),
    "minimax_h3_ref": _template(
        "minimax_h3_ref", "MiniMax H3 (full reference)",
        "MiniMax H3's six full-reference sections, for prompts that carry "
        "reference labels for subjects, pictures, video and audio.",
        _MINIMAX_REF_CHANNELS,
        field_separator="\n\n", label_separator=":\n", labels=LABELS_ALWAYS,
        shot_marker_channel="detailed_description",
        # NOT channel 1. `subject_definitions` leads this template, so the
        # first-channel fallback sent every unheadered line of a narrative
        # draft into the definitions field — the bug this pointer exists to fix.
        default_draft_channel="detailed_description",
        global_merge=GLOBAL_MERGE_PER_CHANNEL,
        default_context_profile="minimax_h3_ref@1",
    ),
}


def _clone_template(template: dict) -> dict:
    """Return a mutation-safe copy of a preset or normalized custom."""
    return {
        **template,
        "channels": tuple(dict(channel) for channel in template.get("channels") or ()),
    }


def get_channel_template(template_id) -> dict:
    """Return the preset for `template_id`, falling back to the default.

    An unknown id resolves to the default rather than raising: a project saved
    against a custom template that was later removed must still compose.
    """
    if isinstance(template_id, dict):
        return normalize_channel_template(template_id)
    key = str(template_id or "") or DEFAULT_CHANNEL_TEMPLATE_ID
    preset = PROMPT_CHANNEL_TEMPLATE_PRESETS.get(key)
    if preset is None:
        preset = PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID]
    return _clone_template(preset)


def strict_normalize_channel_template(raw):
    """Validate a materialized custom template, returning None if malformed."""
    if not isinstance(raw, dict):
        return None
    raw_channels = raw.get("channels")
    if not isinstance(raw_channels, (list, tuple)) or not raw_channels:
        return None

    channels = []
    seen = set()
    for entry in raw_channels:
        if not isinstance(entry, dict):
            return None
        key = str(entry.get("key") or "").strip()
        if not key or key in seen:
            return None
        seen.add(key)
        channels.append(_channel(key,
                                 str(entry.get("label") or ""),
                                 str(entry.get("description") or "")))

    labels = str(raw.get("labels") or LABELS_PROJECT)
    if labels not in (LABELS_ALWAYS, LABELS_NEVER, LABELS_PROJECT):
        return None
    global_merge = str(raw.get("global_merge") or GLOBAL_MERGE_LEADING)
    if global_merge not in (GLOBAL_MERGE_LEADING, GLOBAL_MERGE_PER_CHANNEL):
        return None
    shot_marker_channel = str(raw.get("shot_marker_channel") or "")
    if shot_marker_channel not in seen:
        if shot_marker_channel:
            return None
    draft_channel = str(raw.get("default_draft_channel") or "")
    if draft_channel and draft_channel not in seen:
        return None
    return {
        "id": str(raw.get("id") or "custom"),
        "name": str(raw.get("name") or "Custom"),
        "description": str(raw.get("description") or ""),
        "channels": tuple(channels),
        "field_separator": str(raw.get("field_separator", " ")),
        "label_separator": str(raw.get("label_separator", " ")),
        "labels": labels,
        "shot_marker_channel": shot_marker_channel,
        "default_draft_channel": draft_channel,
        "global_merge": global_merge,
        "global_channels_enabled": raw.get("global_channels_enabled", True) is not False,
        "default_context_profile": str(raw.get("default_context_profile") or "generic@1"),
        "builtin": False,
    }


def normalize_channel_template(raw) -> dict:
    """Coerce a project-owned (forked) template dict into the full shape.

    Anything missing or malformed falls back to the default template's value,
    so a hand-edited project.json cannot produce a template with no channels.
    """
    if not isinstance(raw, dict):
        return _clone_template(PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID])
    channels = []
    seen = set()
    for entry in raw.get("channels") or ():
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        channels.append(dict(entry, key=key))
    labels = str(raw.get("labels") or LABELS_PROJECT)
    if labels not in (LABELS_ALWAYS, LABELS_NEVER, LABELS_PROJECT):
        labels = LABELS_PROJECT
    global_merge = str(raw.get("global_merge") or GLOBAL_MERGE_LEADING)
    if global_merge not in (GLOBAL_MERGE_LEADING, GLOBAL_MERGE_PER_CHANNEL):
        global_merge = GLOBAL_MERGE_LEADING
    marker = str(raw.get("shot_marker_channel") or "")
    if marker not in seen:
        marker = ""
    draft_channel = str(raw.get("default_draft_channel") or "")
    if draft_channel not in seen:
        draft_channel = ""
    normalized = strict_normalize_channel_template({
        **raw,
        "channels": channels,
        "labels": labels,
        "global_merge": global_merge,
        "shot_marker_channel": marker,
        "default_draft_channel": draft_channel,
    })
    if normalized is not None:
        return normalized
    return _clone_template(PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID])


PROJECT_TEMPLATE_KEY = "prompt_channel_template"


def resolve_channel_template(metadata=None, params=None) -> dict:
    """Resolve the effective template for one composition.

    A frozen job's `params` win over live project `metadata`, so a queued job
    re-composes against the template it was enqueued under rather than whatever
    the project carries now. Either source may hold a preset id or a full
    project-owned template dict.
    """
    for source in (params, metadata):
        if not isinstance(source, dict) or not source.get(PROJECT_TEMPLATE_KEY):
            continue
        raw = source[PROJECT_TEMPLATE_KEY]
        resolved = get_channel_template(raw)
        # Compatibility for jobs queued before whole-dict freezing. A bare
        # preset id plus the legacy frozen toggle must keep its enqueue-time
        # label behavior even if the shipped preset policy changed later.
        if (source is params and isinstance(raw, str)
                and raw in PROMPT_CHANNEL_TEMPLATE_PRESETS
                and "prompt_channel_labels" in source):
            resolved = _clone_template(resolved)
            resolved["labels"] = LABELS_PROJECT
        return resolved
    return _clone_template(PROMPT_CHANNEL_TEMPLATE_PRESETS[DEFAULT_CHANNEL_TEMPLATE_ID])


def _template_dict(resolved) -> dict:
    """Serializable complete projection for project-custom and job paths."""
    return {
        "id": resolved["id"],
        "name": resolved["name"],
        "description": resolved.get("description", ""),
        "channels": [dict(channel) for channel in resolved.get("channels") or ()],
        "field_separator": resolved.get("field_separator", " "),
        "label_separator": resolved.get("label_separator", " "),
        "labels": resolved.get("labels", LABELS_PROJECT),
        "shot_marker_channel": resolved.get("shot_marker_channel", ""),
        "default_draft_channel": resolved.get("default_draft_channel", ""),
        "global_merge": resolved.get("global_merge", GLOBAL_MERGE_LEADING),
        "global_channels_enabled": global_channels_enabled(resolved),
        "default_context_profile": resolved.get("default_context_profile", "generic@1"),
    }


def project_template_value(template) -> object:
    """Project projection: preset id for built-ins, full dict for customs."""
    resolved = template if isinstance(template, dict) else get_channel_template(template)
    if resolved.get("builtin"):
        return resolved["id"]
    return _template_dict(resolved)


def template_freeze_value(template) -> dict:
    """Job projection: always the complete resolved template dict."""
    resolved = template if isinstance(template, dict) else get_channel_template(template)
    return _template_dict(resolved)


def template_channel_keys(template) -> tuple:
    """Ordered channel keys for a template (or template id)."""
    resolved = template if isinstance(template, dict) else get_channel_template(template)
    return tuple(channel["key"] for channel in resolved.get("channels") or ())


def global_template_view(template) -> dict:
    """The template as the scene-global prompt sees it.

    Identical when global channels are on; narrowed to the first channel when
    they are off. Collapsing between two of these views is what makes toggling
    the flag a reversible collapse/re-split instead of losing the other
    channels' text.
    """
    resolved = template if isinstance(template, dict) else get_channel_template(template)
    keys = set(global_channel_keys(resolved))
    channels = tuple(dict(channel) for channel in resolved.get("channels") or ()
                     if channel.get("key") in keys)
    view = dict(resolved)
    view["channels"] = channels
    if resolved.get("shot_marker_channel") not in keys:
        view["shot_marker_channel"] = ""
    # Same reason: the narrowed view must not point at a channel it dropped.
    # Blanking is safe here because `default_draft_channel()` falls back to the
    # view's own first channel rather than to nothing.
    if resolved.get("default_draft_channel") not in keys:
        view["default_draft_channel"] = ""
    return view


def template_labels_on(template, project_labels_on: bool) -> bool:
    """Resolve the effective label policy for one composition."""
    resolved = template if isinstance(template, dict) else get_channel_template(template)
    policy = resolved.get("labels", LABELS_PROJECT)
    if policy == LABELS_ALWAYS:
        return True
    if policy == LABELS_NEVER:
        return False
    return bool(project_labels_on)


def format_shot_timecode(frames, fps) -> str:
    """`MM:SS.mmm` for a window-local frame offset. Minutes are never wrapped.

    Milliseconds are rounded from the exact frame/fps ratio, so 84 frames at
    24fps is 00:03.500 rather than a float-truncated 00:03.499.
    """
    try:
        frames = int(frames)
        fps = float(fps)
    except (TypeError, ValueError):
        return ""
    if fps <= 0 or frames < 0:
        return ""
    total_ms = int(round(frames * 1000.0 / fps))
    minutes, remainder = divmod(total_ms, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{minutes:02d}:{seconds:02d}.{milliseconds:03d}"
