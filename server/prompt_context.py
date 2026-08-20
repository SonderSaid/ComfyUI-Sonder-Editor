"""Structured prompt documents, semantic attachments, and pure compilation.

This module is deliberately UI- and Comfy-free.  The timeline, Prompt tool,
queue freezer, Prompt Relay and nodes all consume the same normalized records.
Provider text is produced here at compile time; projects store stable ids and
semantic intent only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from collections import defaultdict

from . import prompt_channel_templates, prompt_payload, prompt_tokens


FORMAT_VERSION = "prompt_context_v1"
DOCUMENT_SCHEMA = "prompt_document_v1"
PROFILE_LIMIT_BYTES = 128 * 1024
FORMATTER_LIMIT_BYTES = 4 * 1024
MAX_CAPABILITIES = 64
MAX_ATTACHMENTS_PER_SCENE = 512
MAX_ATTACHMENT_OUTPUT = 16 * 1024
MAX_COMPILED_PROMPT = 256 * 1024
MAX_PHYSICAL_POPULATIONS = 16
MAX_IDENTITY_KINDS = 16
MAX_CONTRIBUTION_VALUES = 64

SUPPORTED_ATTACHMENT_KINDS = {
    "shot", "timestamp", "prompt_link", "prompt_link_scope", "reference",
    "vocal_event", "custom",
}
MAX_ATTACHMENT_KIND = 64
PLACEMENT_PHASES = (
    "document_preamble", "channel_prefix", "global_document",
    "section_prefix", "inline", "section_suffix", "channel_suffix",
)
PLACEMENT_PHASE_CATALOG = (
    {"value": "document_preamble", "label": "Document preamble",
     "description": "Before every channel and section contribution."},
    {"value": "channel_prefix", "label": "Channel prefix",
     "description": "At the beginning of the destination channel."},
    {"value": "global_document", "label": "Global document",
     "description": "With the scene-global contribution for the destination channel."},
    {"value": "section_prefix", "label": "Section prefix",
     "description": "Before authored text in each effective section."},
    {"value": "inline", "label": "Inline at cursor",
     "description": "At its document anchor; unanchored values follow section prefixes."},
    {"value": "section_suffix", "label": "Section suffix",
     "description": "After authored text in each effective section."},
    {"value": "channel_suffix", "label": "Channel suffix",
     "description": "At the end of the destination channel."},
)
PROFILE_DECLARATION_MAX_DEPTH = 16
FIELD_DECLARATION_TYPES = frozenset({"enum", "enum_multi"})
FIELD_DEFAULT_SOURCES = frozenset({"roles"})
# The fixed server renderer allowlist, ordered and paired with a fallback name.
# It answers "which capability kinds can this build render", not "what does any
# provider call them": a format relabels every kind it declares, and the
# built-ins carry their own authored labels. The names here are only what the
# format editor offers when a kind is first declared, published as
# `reference_capability_kinds` alongside `placement_phases`.
REFERENCE_RENDERER_KIND_LABELS = (
    ("derived_prompt", "Reference prompt"),
    ("definitions", "Definition"),
    ("summary", "Summary"),
    ("retention", "Retention"),
    ("mentions", "Scene mention"),
    ("audio_relationship", "Audio relationship"),
)
REFERENCE_RENDERER_KINDS = frozenset(
    kind for kind, _label in REFERENCE_RENDERER_KIND_LABELS)
SEPARATOR_NAMES = ("attachment", "line")
# A declared separator is a JOINER, never authored content: a custom format may
# choose how its emissions sit against each other, but must not be able to
# inject prose between them. Whitespace only, and a closed set of it.
DECLARED_CAPABILITY_SEPARATORS = frozenset({" ", "\n", "\n\n"})
# Shot ordinals ride the same ordinal manifest as identity and physical
# populations so `@shot(id)` resolves through the one token resolver. Keyed by
# the opening Shot attachment's id, and window-relative like every other
# ordinal — a shot outside the selected window has no number and its citation
# is a blocking dangling binding, not a silent zero.
SHOT_ORDINAL_KEY = "shots"
# Every attachment stores `provider_id` and `provider_version`; the declarative
# profile registry below is the authority for which pairs this build/project can
# interpret. Legacy records normalize to generic@1 and stay valid, and disabled
# chips are never validated because they emit nothing.
# Prompt Links resolve only through inline document anchors, and Vocal Events
# must stay chronological, so both are inline-only in every authoring surface.
INLINE_ONLY_KINDS = frozenset({"prompt_link", "vocal_event"})
SCOPE_ONLY_KINDS = frozenset({"prompt_link_scope"})
VISUAL_INTENTS = {
    "preserve": "fully_preserved",
    "partial": "partially_preserved",
    "transfer_attributes": "attribute_transfer",
    "reference_loosely": "weak_reference",
}
AUDIO_INTENTS = {
    "copy_full": "fully_copy",
    "copy_partial": "partially_copy",
    "reference_characteristics": "reference",
    "reference_loosely": "weak_reference",
}
VOCAL_EVENT_TYPES = {
    "dialogue", "singing", "narration", "voiceover", "group_speech",
}
DIALOGUE_LANGUAGES = {
    "English", "Spanish", "French", "German", "Italian", "Japanese",
    "Korean", "Chinese", "Portuguese", "Hindi",
}
_MANUAL_SPEAKER_RE = re.compile(r"\(S\d+(?:[,+]S\d+)*\)")
MINIMAX_TASK_TYPES = (
    "keyframe completion", "reference generation", "video editing",
    "video continuation", "audio reuse", "audio reference",
)
MINIMAX_TASK_TYPE_CHOICES = [
    {"value": value, "label": value.title()}
    for value in MINIMAX_TASK_TYPES
]

DEFAULT_CONTRIBUTION_CATALOG = {
    "*": [
        {"value": "appearance", "label": "Appearance"},
        {"value": "costume", "label": "Costume"},
        {"value": "motion", "label": "Motion"},
        {"value": "composition", "label": "Composition"},
        {"value": "timbre", "label": "Voice timbre"},
        {"value": "delivery", "label": "Voice delivery"},
    ],
}
VISUAL_INTENT_CHOICES = (
    {"value": "preserve", "label": "Fully preserve"},
    {"value": "partial", "label": "Partially preserve"},
    {"value": "transfer_attributes", "label": "Transfer attributes"},
    {"value": "reference_loosely", "label": "Reference loosely"},
)
AUDIO_INTENT_CHOICES = (
    {"value": "copy_full", "label": "Fully copy"},
    {"value": "copy_partial", "label": "Partially copy"},
    {"value": "reference_characteristics", "label": "Reference audio characteristics"},
    {"value": "reference_loosely", "label": "Reference loosely"},
)

MINIMAX_H3_PHYSICAL_POPULATIONS = [
    {
        "key": "pictures", "ordinal_key": "pictures",
        "source_key": "picture_ids", "token_kind": "picture",
        "label": "Picture", "label_template": "<Picture {n}>",
        "media_kinds": ["image"], "cap": 9,
        "description": "Still-image references presented to MiniMax as <Picture N>.",
    },
    {
        "key": "videos", "ordinal_key": "videos",
        "source_key": "video_ids", "token_kind": "video",
        "label": "Video", "label_template": "<Video {n}>",
        "media_kinds": ["video"], "cap": 3,
        "description": "Video references presented to MiniMax as <Video N>.",
    },
    {
        "key": "standalone_audios", "ordinal_key": "audios",
        "source_key": "audio_ids", "token_kind": "audio",
        "label": "Audio", "label_template": "<Audio {n}>",
        "media_kinds": ["audio", "video"], "cap": 3,
        "description": "Standalone audio references presented as <Audio N>.",
    },
]

MINIMAX_SUBJECT_KIND = {
    "key": "subject", "label": "Subject", "token_kind": "subject",
    "referenced_label_template": "<Subject {n}>",
    "assetless_label_template": "", "speaks": True,
    "description": ("A semantic person, character, object, or other identity; "
                    "it may be description-only or attributed to physical references."),
}

DEFAULT_SPEAKER_POLICY = {
    "enabled": True, "token_template": "(S{n})",
    "compound_join": ",", "compound_order": "authored",
}

# Server-owned staged-member role vocabulary.  Recipes declare which fields
# they expose; the selected prompt format and physical model input declare the
# values those fields accept.  Keeping this catalog here gives the compiler,
# mutation routes, and every frontend renderer one source of truth.
MINIMAX_H3_ROLE_CATALOGS = {
    "pictures": [
        {"value": "first_frame", "label": "First frame"},
        {"value": "last_frame", "label": "Last frame"},
        {"value": "keyframe", "label": "Keyframe"},
        {"value": "storyboard", "label": "Storyboard"},
        {"value": "composition_anchor", "label": "Composition anchor"},
        {"value": "identity", "label": "Identity"},
        {"value": "environment", "label": "Environment"},
        {"value": "style", "label": "Style"},
        {"value": "motion", "label": "Motion"},
    ],
    "videos": [
        {"value": "video_editing", "label": "Video editing"},
        {"value": "video_continuation", "label": "Video continuation"},
        {"value": "temporal_structure", "label": "Temporal structure"},
        {"value": "motion", "label": "Motion / camera"},
    ],
    "standalone_audios": [
        {"value": "audio_reuse", "label": "Audio reuse"},
        {"value": "audio_reference", "label": "Audio reference"},
        {"value": "timbre", "label": "Voice timbre"},
        {"value": "rhythm", "label": "Music / rhythm"},
        {"value": "sound_texture", "label": "Sound texture"},
    ],
}
def _normalized_role_catalog(raw) -> dict:
    result = {}
    if not isinstance(raw, dict) or len(raw) > 16:
        return result
    for population, entries in raw.items():
        population = str(population or "").strip()
        if not population or not isinstance(entries, list) or len(entries) > 64:
            continue
        values = []
        seen = set()
        for entry in entries:
            if isinstance(entry, str):
                value, label = entry.strip(), entry.strip()
            elif isinstance(entry, dict):
                value = str(entry.get("value") or "").strip()
                label = str(entry.get("label") or value).strip()
            else:
                continue
            if (not value or len(value) > 128 or len(label) > 128
                    or value in seen):
                continue
            seen.add(value)
            values.append({"value": value, "label": label})
        if values:
            result[population] = values
    return result


def reference_role_catalog(profile_ids=None, population="", *, profiles=None,
                           active_profile="") -> list[dict]:
    profile_keys = {str(value or "") for value in (profile_ids or [])}
    if active_profile:
        active_key = str(active_profile)
        if active_key not in profile_keys:
            return []
        profile_keys = {active_key}
    candidates = [*BUILTIN_PROFILES.values(), *(profiles or [])]
    for profile in candidates:
        if not isinstance(profile, dict):
            continue
        key = f"{profile.get('profile_id', '')}@{profile.get('version', '1')}"
        if key in profile_keys:
            return copy.deepcopy((_normalized_role_catalog(
                profile.get("role_catalogs"))).get(str(population), []))
    return []


def reference_capability_catalog(profile_ids=None, *, profiles=None) -> list[str]:
    """Union derived capability names from the recipe's compatible formats.

    A Reference recipe may target several prompt formats, so no single active
    format owns its durable `exposed_capabilities` vocabulary.  The bounded
    union of those explicitly compatible profiles is the one authoring and
    validation authority.
    """
    profile_keys = {str(value or "") for value in (profile_ids or [])
                    if str(value or "")}
    if not profile_keys:
        profile_keys = {"generic@1"}
    result = []
    seen = set()
    for profile in [*BUILTIN_PROFILES.values(), *(profiles or [])]:
        if not isinstance(profile, dict):
            continue
        key = f"{profile.get('profile_id', '')}@{profile.get('version', '1')}"
        if key not in profile_keys:
            continue
        for capability in _reference_derived_view(profile):
            if capability and capability not in seen:
                seen.add(capability)
                result.append(capability)
    return result


def normalize_reference_role(role, recipe, *, profiles=None,
                             active_profile="") -> str:
    role = str(role or "").strip()
    if not role:
        return ""
    soft = recipe.get("soft") if isinstance(recipe, dict) else {}
    soft = soft if isinstance(soft, dict) else {}
    profile_ids = soft.get("compatible_profiles") or []
    population = str(soft.get("physical_population") or "")
    catalog = reference_role_catalog(
        profile_ids, population, profiles=profiles,
        active_profile=active_profile)
    if not catalog:
        # Deserialization preserves an unknown saved role before its project
        # profile is available.  A project mutation receives `profiles` (even
        # when empty) and must instead refuse authoring a value with no catalog.
        if profiles is not None:
            raise ValueError(f"Unsupported Reference role: {role}")
        if len(role) > 128:
            raise ValueError("Reference role is too long")
        return role
    if role not in {entry["value"] for entry in catalog}:
        raise ValueError(f"Unsupported Reference role: {role}")
    return role


_DECLARATION_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
PROMPT_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


def normalize_prompt_handle(value) -> str:
    """Preserve authored spelling; syntax/uniqueness are mutation concerns."""
    return str(value or "").strip()


def prompt_handle_error(value) -> str:
    handle = normalize_prompt_handle(value)
    if handle and not PROMPT_HANDLE_RE.fullmatch(handle):
        return ("Handles must start with a letter and use at most 64 letters, "
                "numbers, or underscores.")
    return ""


def _declaration_template_valid(value, *, allow_empty=False) -> bool:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 256:
        return False
    if not value:
        return allow_empty
    fields = set(re.findall(r"\{([^{}]+)\}", value))
    if fields != {"n"}:
        return False
    try:
        value.format(n=1)
    except (KeyError, ValueError, IndexError):
        return False
    return True


def _declared_choice_value(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("value") or "")
    return ""


def _declared_choice_label(entry) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return str(entry.get("label") or entry.get("value") or "")
    return ""


def _reference_derived_view(profile) -> dict:
    reference = ((profile or {}).get("capabilities") or {}).get("reference")
    derived = reference.get("derived") if isinstance(reference, dict) else None
    return derived if isinstance(derived, dict) else {}


def reference_derived_declarations(profile) -> dict:
    return copy.deepcopy(_reference_derived_view(profile))


def declared_reference_field(profile, capability_kind, field_name) -> dict:
    declaration = _reference_derived_view(profile).get(
        str(capability_kind or ""))
    fields = declaration.get("fields") if isinstance(declaration, dict) else None
    field = fields.get(str(field_name or "")) if isinstance(fields, dict) else None
    return copy.deepcopy(field) if isinstance(field, dict) else {}


def declared_field_choices(field) -> list[dict]:
    values = field.get("values") if isinstance(field, dict) else None
    result = []
    for entry in values if isinstance(values, list) else []:
        value = _declared_choice_value(entry).strip()
        label = _declared_choice_label(entry).strip()
        if not value or not label:
            continue
        result.append({
            "value": value,
            "label": label,
            **({"description": str(entry.get("description") or "")}
               if isinstance(entry, dict)
               and str(entry.get("description") or "") else {}),
        })
    return result


def _validate_declared_field(raw, *, field, add):
    if not isinstance(raw, dict):
        add("incomplete_capability_declaration",
            "A capability field declaration must be an object.", field)
        return
    field_type = str(raw.get("type") or "")
    if field_type not in FIELD_DECLARATION_TYPES:
        add("incomplete_capability_declaration",
            "Capability fields must use enum or enum_multi.", field)
    unknown = set(raw).difference({
        "type", "values", "label", "help", "example", "default_source",
        "optional",
    })
    if unknown:
        add("incomplete_capability_declaration",
            f"Unsupported capability field keys: {', '.join(sorted(unknown))}.",
            field)
    values = raw.get("values")
    if not isinstance(values, list) or not values or len(values) > 64:
        add("incomplete_capability_declaration",
            "Capability field values must be a bounded nonempty list.", field)
    else:
        seen = set()
        for index, entry in enumerate(values):
            choice_field = f"{field}.values[{index}]"
            value = _declared_choice_value(entry)
            label = _declared_choice_label(entry)
            if (not value or len(value) > 128 or value in seen
                    or not label or len(label) > 128):
                add("incomplete_capability_declaration",
                    "Capability field choices need unique bounded values and labels.",
                    choice_field)
            seen.add(value)
            if isinstance(entry, dict):
                unknown_choice = set(entry).difference(
                    {"value", "label", "description"})
                description = entry.get("description", "")
                if (unknown_choice or not isinstance(description, str)
                        or len(description.encode("utf-8")) > FORMATTER_LIMIT_BYTES):
                    add("incomplete_capability_declaration",
                        "Capability field choice metadata is invalid.", choice_field)
            elif not isinstance(entry, str):
                add("incomplete_capability_declaration",
                    "Capability field choices must be strings or value-label objects.",
                    choice_field)
    for name in ("label", "help", "example"):
        text = raw.get(name, "")
        if (not isinstance(text, str)
                or len(text.encode("utf-8")) > FORMATTER_LIMIT_BYTES):
            add("incomplete_capability_declaration",
                f"Capability field {name} is invalid.", field)
    default_source = raw.get("default_source")
    if (default_source is not None
            and (not isinstance(default_source, str)
                 or default_source not in FIELD_DEFAULT_SOURCES)):
        add("unknown_field_default_source",
            f"Unknown capability-field default source: {default_source!r}.", field)
    # A declared vocabulary cannot express "leave this out": every choice needs
    # a nonempty value, so an empty option is not declarable. `optional` is how
    # a format says the field may be omitted — the H3 camera grammar omits
    # amplitude and speed unless they are meaningful — and the authoring surface
    # offers a not-set choice only for these. Absent means required.
    optional = raw.get("optional")
    if optional is not None and not isinstance(optional, bool):
        add("incomplete_capability_declaration",
            "Capability field optional must be a boolean.", field)


def _bound_template_channel_keys(value, template):
    """The channel keys a declaration may name, or None when unknowable.

    A format is only measured against a template it is actually bound to: a
    profile validated beside some other project's template would otherwise
    report every one of its own channels as missing. `None` means "no opinion",
    which is what an unbound or absent template earns.
    """
    template_id = str((template or {}).get("id") or "")
    declared_template_id = str(value.get("template_id") or "")
    declared_templates = value.get("compatible_templates")
    template_is_bound = (
        not isinstance(template, dict)
        or template_id == declared_template_id
        or (isinstance(declared_templates, list)
            and template_id in {str(entry) for entry in declared_templates})
        or str((template or {}).get("default_context_profile") or "") in {
            f"{value.get('profile_id')}@{value.get('version', '1')}",
            str(value.get("profile_id") or ""),
        }
    )
    if isinstance(template, dict) and template_is_bound:
        return {str(row.get("key") or "")
                for row in template.get("channels") or ()
                if isinstance(row, dict)}
    return None


def _writing_aid_declaration_errors(value, *, template, add):
    """Writing-aid channel targeting, measured against the bound template.

    Unlike a capability's `channel_key`, which routes emitted output, this only
    decides where an aid is OFFERED. A key naming no channel would silently hide
    the aid everywhere, which is worse than a loud declaration error.
    """
    aids = value.get("writing_aids")
    if not isinstance(aids, list):
        return
    channel_keys = _bound_template_channel_keys(value, template)
    if channel_keys is None:
        return
    for index, aid in enumerate(aids):
        if not isinstance(aid, dict):
            continue
        declared = aid.get("channel_keys")
        if not isinstance(declared, list):
            continue
        field = f"writing_aids[{index}].channel_keys"
        for key in declared:
            if str(key) not in channel_keys:
                add("incomplete_capability_declaration",
                    f"Writing aid names missing template channel {str(key)!r}.",
                    field)


def _reference_declaration_errors(value, *, template, add):
    reference = ((value.get("capabilities") or {}).get("reference"))
    if not isinstance(reference, dict):
        return
    derived = reference.get("derived")
    if not isinstance(derived, dict) or not derived:
        add("incomplete_capability_declaration",
            "Reference capabilities require a nonempty derived declaration.",
            "capabilities.reference.derived")
        return
    if len(derived) > MAX_CAPABILITIES:
        add("incomplete_capability_declaration",
            f"A format may declare at most {MAX_CAPABILITIES} derived capabilities.",
            "capabilities.reference.derived")
    channel_keys = _bound_template_channel_keys(value, template)
    seen_orders = set()
    for kind, declaration in derived.items():
        field = f"capabilities.reference.derived.{kind}"
        if (not isinstance(kind, str) or not _DECLARATION_KEY_RE.fullmatch(kind)
                or not isinstance(declaration, dict)):
            add("incomplete_capability_declaration",
                "Derived capability keys and declarations must be bounded objects.",
                field)
            continue
        if kind not in REFERENCE_RENDERER_KINDS:
            add("incomplete_capability_declaration",
                f"Derived capability {kind!r} has no bounded server renderer.",
                field)
        unknown = set(declaration).difference({
            "order", "channel_key", "placement", "label", "description",
            "example", "help", "fields", "separator",
        })
        if unknown:
            add("incomplete_capability_declaration",
                f"Unsupported derived capability fields: {', '.join(sorted(unknown))}.",
                field)
        separator = declaration.get("separator")
        if (separator is not None
                and separator not in DECLARED_CAPABILITY_SEPARATORS):
            add("incomplete_capability_declaration",
                "Derived capability separator must be one of the declared "
                "whitespace joiners.", field)
        order = declaration.get("order")
        if (not isinstance(order, int) or isinstance(order, bool)
                or not 0 <= order <= MAX_CAPABILITIES or order in seen_orders):
            add("incomplete_capability_declaration",
                "Derived capability order must be a unique bounded integer.", field)
        else:
            seen_orders.add(order)
        channel_key = declaration.get("channel_key")
        label = declaration.get("label")
        if (not isinstance(channel_key, str) or not channel_key.strip()
                or len(channel_key) > 128
                or not isinstance(label, str) or not label.strip()
                or len(label) > 128):
            add("incomplete_capability_declaration",
                "Derived capabilities require bounded channel_key and label values.",
                field)
        elif channel_keys is not None and channel_key not in channel_keys:
            add("incomplete_capability_declaration",
                f"Derived capability names missing template channel {channel_key!r}.",
                field)
        if declaration.get("placement") not in PLACEMENT_PHASES:
            add("incomplete_capability_declaration",
                "Derived capability placement is unsupported.", field)
        for name in ("description", "example", "help"):
            text = declaration.get(name, "")
            if (not isinstance(text, str)
                    or len(text.encode("utf-8")) > FORMATTER_LIMIT_BYTES):
                add("incomplete_capability_declaration",
                    f"Derived capability {name} is invalid.", field)
        fields = declaration.get("fields", {})
        if not isinstance(fields, dict) or len(fields) > 64:
            add("incomplete_capability_declaration",
                "Derived capability fields must be a bounded object.", field)
            continue
        for name, field_declaration in fields.items():
            if not isinstance(name, str) or not _DECLARATION_KEY_RE.fullmatch(name):
                add("incomplete_capability_declaration",
                    "Capability field names must be bounded lowercase identifiers.",
                    f"{field}.fields.{name}")
                continue
            _validate_declared_field(
                field_declaration, field=f"{field}.fields.{name}", add=add)


def profile_declaration_errors(profile, *, template=None) -> list[dict]:
    """Validate format-owned static declarations without rewriting them.

    Project loading preserves unknown and over-cap declarations so the author can
    repair them. Authoring and profile resolution call this helper and refuse the
    invalid declaration instead of silently dropping intent.
    """
    value = profile if isinstance(profile, dict) else {}
    errors = []

    def add(code, message, field=""):
        errors.append({"code": code, "message": message,
                       **({"field": field} if field else {})})

    _reference_declaration_errors(value, template=template, add=add)
    _writing_aid_declaration_errors(value, template=template, add=add)

    populations = value.get("physical_populations", [])
    if not isinstance(populations, list):
        add("unsupported_population", "Physical populations must be a list.",
            "physical_populations")
        populations = []
    elif len(populations) > MAX_PHYSICAL_POPULATIONS:
        add("unsupported_population",
            f"A prompt format may declare at most {MAX_PHYSICAL_POPULATIONS} physical populations.",
            "physical_populations")
    seen_population_fields = {name: set() for name in (
        "key", "ordinal_key", "source_key", "token_kind")}
    population_keys = set()
    allowed_population_fields = {
        "key", "ordinal_key", "source_key", "token_kind", "label",
        "label_template", "media_kinds", "cap", "description",
    }
    for index, raw in enumerate(populations):
        field = f"physical_populations[{index}]"
        if not isinstance(raw, dict):
            add("unsupported_population", "A physical population must be an object.", field)
            continue
        unknown = set(raw).difference(allowed_population_fields)
        if unknown:
            add("unsupported_population",
                f"Unsupported physical population fields: {', '.join(sorted(unknown))}.", field)
        for name in ("key", "ordinal_key", "source_key", "token_kind"):
            token = raw.get(name)
            if not isinstance(token, str) or not _DECLARATION_KEY_RE.fullmatch(token):
                add("unsupported_population", f"{name} must be a bounded lowercase identifier.", field)
                continue
            if token in seen_population_fields[name]:
                add("unsupported_population", f"Duplicate physical population {name}: {token}.", field)
            seen_population_fields[name].add(token)
        key = str(raw.get("key") or "")
        if key:
            population_keys.add(key)
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 128:
            add("unsupported_population", "Physical population label is invalid.", field)
        description = raw.get("description", "")
        if (not isinstance(description, str)
                or len(description.encode("utf-8")) > FORMATTER_LIMIT_BYTES):
            add("unsupported_population", "Physical population description is invalid.", field)
        if not _declaration_template_valid(raw.get("label_template")):
            add("unsupported_population", "Physical label_template must contain only {n}.", field)
        media_kinds = raw.get("media_kinds")
        if (not isinstance(media_kinds, list) or not media_kinds
                or len(media_kinds) > 8
                or any(not isinstance(kind, str)
                       or not _DECLARATION_KEY_RE.fullmatch(kind)
                       for kind in media_kinds)
                or len(set(media_kinds)) != len(media_kinds)):
            add("unsupported_population", "Physical media_kinds must be a unique bounded set.", field)
        cap = raw.get("cap")
        if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= 64:
            add("unsupported_population", "Physical population cap must be between 1 and 64.", field)

    identity_kinds = value.get("identity_kinds", [])
    if not isinstance(identity_kinds, list):
        add("unsupported_identity_kind", "Identity kinds must be a list.", "identity_kinds")
        identity_kinds = []
    elif len(identity_kinds) > MAX_IDENTITY_KINDS:
        add("unsupported_identity_kind",
            f"A prompt format may declare at most {MAX_IDENTITY_KINDS} identity kinds.",
            "identity_kinds")
    seen_identity = {name: set() for name in ("key", "token_kind")}
    allowed_identity_fields = {
        "key", "label", "token_kind", "referenced_label_template",
        "assetless_label_template", "speaks", "description",
    }
    for index, raw in enumerate(identity_kinds):
        field = f"identity_kinds[{index}]"
        if not isinstance(raw, dict):
            add("unsupported_identity_kind", "An identity kind must be an object.", field)
            continue
        unknown = set(raw).difference(allowed_identity_fields)
        if unknown:
            add("unsupported_identity_kind",
                f"Unsupported identity-kind fields: {', '.join(sorted(unknown))}.", field)
        for name in ("key", "token_kind"):
            token = raw.get(name)
            if not isinstance(token, str) or not _DECLARATION_KEY_RE.fullmatch(token):
                add("unsupported_identity_kind", f"{name} must be a bounded lowercase identifier.", field)
                continue
            if token in seen_identity[name]:
                add("unsupported_identity_kind", f"Duplicate identity {name}: {token}.", field)
            seen_identity[name].add(token)
        label = raw.get("label")
        if not isinstance(label, str) or not label.strip() or len(label) > 128:
            add("unsupported_identity_kind", "Identity-kind label is invalid.", field)
        description = raw.get("description", "")
        if (not isinstance(description, str)
                or len(description.encode("utf-8")) > FORMATTER_LIMIT_BYTES):
            add("unsupported_identity_kind", "Identity-kind description is invalid.", field)
        if not _declaration_template_valid(raw.get("referenced_label_template"), allow_empty=True):
            add("unsupported_identity_kind",
                "referenced_label_template must be empty or contain only {n}.", field)
        if not _declaration_template_valid(raw.get("assetless_label_template"), allow_empty=True):
            add("unsupported_identity_kind",
                "assetless_label_template must be empty or contain only {n}.", field)
        if not isinstance(raw.get("speaks"), bool):
            add("unsupported_identity_kind", "Identity-kind speaks must be boolean.", field)

    if "contribution_catalog" in value:
        catalog = value.get("contribution_catalog")
        if not isinstance(catalog, dict) or len(catalog) > MAX_PHYSICAL_POPULATIONS + 1:
            add("unknown_contribution", "Contribution catalog must be a bounded object.",
                "contribution_catalog")
            catalog = {}
        for population, entries in catalog.items():
            field = f"contribution_catalog.{population}"
            if population != "*" and population not in population_keys:
                add("unknown_contribution",
                    f"Contribution catalog names unknown population {population!r}.", field)
            if (not isinstance(entries, list)
                    or len(entries) > MAX_CONTRIBUTION_VALUES):
                add("unknown_contribution", "Contribution values must be a bounded list.", field)
                continue
            seen = set()
            for entry in entries:
                if not isinstance(entry, dict):
                    add("unknown_contribution", "A contribution value must be an object.", field)
                    continue
                token = entry.get("value")
                label = entry.get("label")
                if (not isinstance(token, str)
                        or not _DECLARATION_KEY_RE.fullmatch(token)
                        or token in seen
                        or not isinstance(label, str) or not label.strip()
                        or len(label) > 128):
                    add("unknown_contribution", "Contribution value or label is invalid.", field)
                    continue
                seen.add(token)

    policy = value.get("speaker_policy")
    if policy is not None:
        if not isinstance(policy, dict):
            add("invalid_speaker_policy", "Speaker policy must be an object.", "speaker_policy")
        else:
            unknown = set(policy).difference({
                "enabled", "token_template", "compound_join", "compound_order"})
            if unknown:
                add("invalid_speaker_policy",
                    f"Unsupported speaker policy fields: {', '.join(sorted(unknown))}.",
                    "speaker_policy")
            if not isinstance(policy.get("enabled"), bool):
                add("invalid_speaker_policy", "Speaker policy enabled must be boolean.",
                    "speaker_policy")
            if not _declaration_template_valid(policy.get("token_template"), allow_empty=True):
                add("invalid_speaker_policy", "Speaker token_template must contain only {n}.",
                    "speaker_policy")
            joiner = policy.get("compound_join")
            if not isinstance(joiner, str) or len(joiner) > 8:
                add("invalid_speaker_policy", "Speaker compound_join is invalid.",
                    "speaker_policy")
            compound_order = policy.get("compound_order")
            if (not isinstance(compound_order, str)
                    or compound_order not in {"authored", "ascending"}):
                add("invalid_speaker_policy", "Speaker compound_order is invalid.",
                    "speaker_policy")
    return errors


def physical_population_declarations(profile) -> list[dict]:
    values = (profile or {}).get("physical_populations")
    return copy.deepcopy(values) if isinstance(values, list) else []


def identity_kind_declarations(profile) -> list[dict]:
    values = (profile or {}).get("identity_kinds")
    return copy.deepcopy(values) if isinstance(values, list) else []


def effective_contribution_catalog(profile) -> dict:
    if "contribution_catalog" not in (profile or {}):
        return copy.deepcopy(DEFAULT_CONTRIBUTION_CATALOG)
    value = (profile or {}).get("contribution_catalog")
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def effective_speaker_policy(profile) -> dict:
    value = (profile or {}).get("speaker_policy")
    if not isinstance(value, dict):
        return {"enabled": False, "token_template": "",
                "compound_join": ",", "compound_order": "authored"}
    return copy.deepcopy(value)


def resolved_profile_definition(profile) -> dict:
    """Catalog projection after server-owned declaration inheritance."""
    resolved = profile_fork_seed(profile)
    resolved["contribution_catalog"] = effective_contribution_catalog(profile)
    resolved["speaker_policy"] = effective_speaker_policy(profile)
    return resolved


def physical_population_by(profile, field, value):
    target = str(value or "")
    return next((entry for entry in physical_population_declarations(profile)
                 if isinstance(entry, dict)
                 and str(entry.get(field) or "") == target), None)


def identity_kind_for(profile, kind="subject") -> dict | None:
    target = str(kind or "subject")
    values = identity_kind_declarations(profile)
    return next((entry for entry in values if isinstance(entry, dict)
                 and str(entry.get("key") or "") == target), None)


def declared_label(declaration, number, field="label_template") -> str:
    template = str((declaration or {}).get(field) or "")
    if not template or not number:
        return ""
    try:
        return template.format(n=int(number))
    except (TypeError, ValueError, KeyError, IndexError):
        return ""


def declared_label_prefix(declaration, field="label_template") -> str:
    template = str((declaration or {}).get(field) or "")
    return template.split("{n}", 1)[0] if "{n}" in template else template


def identity_ordinal_key(kind) -> str:
    key = str(kind or "subject")
    return key if key.endswith("s") else f"{key}s"


def prompt_token_declarations(profile) -> dict:
    """Return the resolved format's stable-token grammar."""
    result = {}
    for declaration in identity_kind_declarations(profile):
        token_kind = str(declaration.get("token_kind") or "")
        if token_kind:
            result[token_kind] = {
                "manifest_key": identity_ordinal_key(declaration.get("key")),
                "label_template": str(
                    declaration.get("referenced_label_template") or ""),
                "physical": False,
            }
    for declaration in physical_population_declarations(profile):
        token_kind = str(declaration.get("token_kind") or "")
        # Parity with `web/js/prompt_tokens.js`: the kind is lowercased and a
        # declaration missing either half of its grammar is skipped. A format
        # declaring `"Shot"` used to register as `shot` in the browser and
        # `Shot` here, and one missing its label template registered a token
        # that could never render.
        manifest_key = str(declaration.get("ordinal_key") or "")
        label_template = str(declaration.get("label_template") or "")
        if token_kind and manifest_key and label_template:
            result[token_kind.lower()] = {
                "manifest_key": manifest_key,
                "label_template": label_template,
                "physical": True,
                "population": str(declaration.get("key") or ""),
            }
    # Shot is neither an identity nor a physical population — it is compiler-owned
    # ordering over the selected window — so it enters the grammar from its own
    # capability declaration. The format decides WHETHER `@shot` exists; the
    # spelling comes from `prompt_payload.SHOT_LABEL_TEMPLATE`, the same constant
    # that renders the marker being cited, so the two cannot drift (PR-20).
    shot_declaration = ((profile or {}).get("capabilities") or {}).get("shot")
    shot_token_kind = str((shot_declaration or {}).get("token_kind") or "") \
        if isinstance(shot_declaration, dict) else ""
    if shot_token_kind:
        result[shot_token_kind] = {
            "manifest_key": SHOT_ORDINAL_KEY,
            "label_template": prompt_payload.SHOT_LABEL_TEMPLATE,
            "physical": False,
        }
    return result


def _new_id() -> str:
    return uuid.uuid4().hex


def _canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def content_hash(value) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def text_document(text="", *, node_id=None) -> dict:
    return {
        "schema": DOCUMENT_SCHEMA,
        "nodes": [{"type": "text", "node_id": node_id or _new_id(),
                   "text": str(text or "")}],
    }


def normalize_prompt_document(raw=None, fallback_text="") -> dict:
    """Return a bounded, ordered text/attachment document.

    Unknown node types are intentionally ignored.  Empty documents retain one
    text node so caret mapping always has a stable landing position.
    """
    if isinstance(raw, str):
        return text_document(raw)
    nodes = []
    seen = set()
    for entry in (raw.get("nodes", []) if isinstance(raw, dict) else []):
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "")
        if kind not in {"text", "attachment"}:
            continue
        node_id = str(entry.get("node_id") or "").strip() or _new_id()
        while node_id in seen:
            node_id = _new_id()
        seen.add(node_id)
        if kind == "text":
            # Parity with `normalizePromptDocument`. Python's `.` DOES match a
            # CR, so the header pattern here matched while the browser's did
            # not — the two splitters disagreed on identical input, and this
            # side kept the CR inside the captured channel text. Strip on both.
            nodes.append({"type": "text", "node_id": node_id,
                          "text": str(entry.get("text") or "").replace(
                              "\r\n", "\n").replace("\r", "\n")})
        else:
            attachment_id = str(entry.get("attachment_id") or "").strip()
            if not attachment_id:
                continue
            node = {"type": "attachment", "node_id": node_id,
                    "attachment_id": attachment_id}
            capability_id = str(entry.get("capability_id") or "").strip()
            if capability_id:
                node["capability_id"] = capability_id
            nodes.append(node)
    if not nodes:
        return text_document(fallback_text)
    return {"schema": DOCUMENT_SCHEMA, "nodes": nodes}


def prompt_document_text(document) -> str:
    document = normalize_prompt_document(document)
    return "".join(node["text"] for node in document["nodes"]
                   if node["type"] == "text")


def document_has_anchors(document) -> bool:
    return any(node.get("type") == "attachment"
               for node in normalize_prompt_document(document)["nodes"])


def normalize_channel_documents(raw, channels=None, keys=None) -> dict:
    source = raw if isinstance(raw, dict) else {}
    mirrors = channels if isinstance(channels, dict) else {}
    ordered = list(keys or ())
    for key in mirrors:
        if str(key) not in ordered:
            ordered.append(str(key))
    for key in source:
        if str(key) not in ordered:
            ordered.append(str(key))
    return {
        key: normalize_prompt_document(source.get(key), mirrors.get(key, ""))
        for key in ordered
    }


def channel_document_mirrors(documents) -> dict:
    return {str(key): prompt_document_text(value)
            for key, value in (documents or {}).items()}


def join_channel_documents(documents, keys) -> dict:
    """Collapse channel documents without flattening attachment anchors."""
    ordered = [str(value) for value in keys or []]
    populated = []
    for key in ordered:
        document = normalize_prompt_document((documents or {}).get(key))
        if prompt_document_text(document).strip() or document_has_anchors(document):
            populated.append((key, document))
    nodes = []
    for index, (key, document) in enumerate(populated):
        if len(ordered) > 1:
            nodes.append({"type": "text", "node_id": _new_id(),
                          "text": f"{key}:\n"})
        nodes.extend(copy.deepcopy(document["nodes"]))
        if index < len(populated) - 1:
            nodes.append({"type": "text", "node_id": _new_id(), "text": "\n\n"})
    return normalize_prompt_document({"nodes": nodes})


def split_document_channels(document, keys) -> dict:
    """Re-split `key:` header lines while keeping anchors in their block."""
    ordered = [str(value) for value in keys or []] or ["visual"]
    key_set = set(ordered)
    output = {key: [] for key in ordered}
    current = ordered[0]

    def append_text(value):
        if not value:
            return
        target = output[current]
        if target and target[-1].get("type") == "text":
            target[-1]["text"] += value
        else:
            target.append({"type": "text", "node_id": _new_id(), "text": value})

    def trim_structural_separator():
        """Remove the blank-line joiner immediately before a parsed header."""
        target = output[current]
        remaining = 2
        while target and remaining and target[-1].get("type") == "text":
            text = str(target[-1].get("text") or "")
            removed = 0
            while text.endswith("\n") and removed < remaining:
                text = text[:-1]
                removed += 1
            remaining -= removed
            if text:
                target[-1]["text"] = text
                break
            target.pop()

    for node in normalize_prompt_document(document)["nodes"]:
        if node["type"] == "attachment":
            output[current].append(copy.deepcopy(node))
            continue
        lines = str(node.get("text") or "").split("\n")
        for index, line in enumerate(lines):
            match = re.match(r"^([A-Za-z0-9_]+):[ \t]?(.*)$", line)
            is_header = bool(match and match.group(1) in key_set)
            if is_header:
                trim_structural_separator()
                current = match.group(1)
                append_text(match.group(2))
            else:
                append_text(line)
            if index < len(lines) - 1 and not is_header:
                append_text("\n")
    return {key: normalize_prompt_document({"nodes": nodes})
            for key, nodes in output.items()}


def retarget_channel_documents(documents, from_keys, to_keys) -> dict:
    """Atomic document-aware collapse/re-split used by template switching."""
    normalized = normalize_channel_documents(documents)
    if not any(prompt_document_text(value).strip() or document_has_anchors(value)
               for value in normalized.values()):
        return normalized
    retargeted = split_document_channels(join_channel_documents(normalized, from_keys),
                                         to_keys)
    # Preserve the historical superset contract: inactive channels remain as
    # empty mirrors so switching away and back never truncates project data.
    for key in normalized:
        if key not in retargeted:
            original = normalized[key]
            node_id = next((node.get("node_id") for node in original.get("nodes", [])
                            if node.get("type") == "text"), None)
            retargeted[key] = text_document("", node_id=node_id)
    return retargeted


def replace_document_text(document, text) -> dict:
    """Replace a flat document, refusing to silently delete inline chips."""
    if document_has_anchors(document):
        raise ValueError("structured_edit_conflict")
    normalized = normalize_prompt_document(document)
    node_id = normalized["nodes"][0]["node_id"]
    return text_document(text, node_id=node_id)


def normalize_capability(raw, *, index=0) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    placement = str(raw.get("placement") or "")
    result = {
        "capability_id": str(raw.get("capability_id") or "").strip()
                         or f"capability_{index + 1}",
        "kind": str(raw.get("kind") or "text"),
        "channel_key": str(raw.get("channel_key") or ""),
        "placement": placement,
        "config": copy.deepcopy(raw.get("config"))
                  if isinstance(raw.get("config"), dict) else {},
    }
    # `enabled` is TRI-STATE and as sparse as the routing beside it: absent
    # inherits the shared Reference/identity default, True/False is an authored
    # chip deviation. Coercing absence to True here erased the distinction at
    # the deserialization boundary, so no resolver downstream could ever see
    # "inherit" and a shared default could never take effect.
    #
    # DELIBERATELY NOT MIGRATED. Projects written before this change carry an
    # eagerly seeded `enabled: true` on every capability (249 such records in
    # the test project, 22 of them a genuine `false`). Stripping `true` at load
    # would be safe only for that pre-change data: from now on `true` is
    # meaningful — it is how a chip stays on against a shared default that is
    # off — and no value-level test can tell the two apart. The residue is
    # bounded and self-healing: such a record simply keeps the part on, and
    # `sparseCapabilityRecord` drops it as redundant the next time that chip is
    # saved. Preferring that over a migration that could silently eat a
    # deliberate override later.
    if raw.get("enabled") is not None:
        result["enabled"] = raw.get("enabled") is not False
    return result


REFERENCE_OVERRIDE_FIELDS = frozenset({
    "definition", "summary", "task_types", "retention_detail",
    "retention_details", "audio_definition", "audio_relationship", "text",
    "visual_intent", "audio_intent",
})


def _normalize_reference_config(raw) -> dict:
    """Migrate flat released fields into the sparse override bag on load.

    The old shape is accepted only at this normalization boundary; all compiler
    paths consume the new bag. A normal project save therefore writes one shape
    instead of keeping two runtime authorities indefinitely.
    """
    config = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    existing = config.get("overrides")
    overrides = copy.deepcopy(existing) if isinstance(existing, dict) else {}
    for field in REFERENCE_OVERRIDE_FIELDS:
        if field in config and field not in overrides:
            overrides[field] = config[field]
        config.pop(field, None)
    config["overrides"] = overrides
    return config


def normalize_attachment(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    attachment_id = str(raw.get("attachment_id") or "").strip() or _new_id()
    kind = str(raw.get("kind") or "custom").strip() or "custom"
    if len(kind) > MAX_ATTACHMENT_KIND:
        kind = kind[:MAX_ATTACHMENT_KIND]
    # Over-cap declarations are preserved, never sliced: deserialization must
    # not silently discard authored data, and `attachment_limit_errors` reports
    # the overflow where a controlled validation error can be returned.
    capabilities = [normalize_capability(value, index=index)
                    for index, value in enumerate(raw.get("capabilities") or [])
                    if isinstance(value, dict)]
    config = (copy.deepcopy(raw.get("config"))
              if isinstance(raw.get("config"), dict) else {})
    if kind == "reference":
        config = _normalize_reference_config(config)
    return {
        "attachment_id": attachment_id,
        "emission_group_id": str(raw.get("emission_group_id") or "").strip()
                             or attachment_id,
        "kind": kind,
        "provider_id": str(raw.get("provider_id") or "generic"),
        "provider_version": str(raw.get("provider_version") or "1"),
        "enabled": raw.get("enabled") is not False,
        "source": copy.deepcopy(raw.get("source"))
                  if isinstance(raw.get("source"), dict) else {},
        "config": config,
        "capabilities": capabilities,
        # Prompt Links export their own dependency edge by default so a linked
        # source remains transitive. Other inline chips stay excluded unless
        # the author explicitly opts them in.
        "link_exportable": bool(raw.get(
            "link_exportable", kind in {"prompt_link", "prompt_link_scope"})),
    }


def normalize_attachments(raw) -> list:
    result = []
    seen = set()
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        normalized = normalize_attachment(entry)
        attachment_id = normalized["attachment_id"]
        if attachment_id in seen:
            normalized["attachment_id"] = _new_id()
        seen.add(normalized["attachment_id"])
        result.append(normalized)
    return result


def attachment_limit_errors(attachments) -> list[dict]:
    """Report over-cap attachment/capability counts as controlled diagnostics."""
    values = [value for value in (attachments if isinstance(attachments, list)
                                  else []) if isinstance(value, dict)]
    errors = []
    if len(values) > MAX_ATTACHMENTS_PER_SCENE:
        errors.append({
            "code": "attachment_limit",
            "message": (f"A scene may contain at most {MAX_ATTACHMENTS_PER_SCENE} "
                        "Context attachments."),
        })
    for value in values:
        capabilities = value.get("capabilities")
        if isinstance(capabilities, list) and len(capabilities) > MAX_CAPABILITIES:
            errors.append({
                "code": "capability_limit",
                "attachment_id": str(value.get("attachment_id") or ""),
                "message": (f"A Context attachment may declare at most "
                            f"{MAX_CAPABILITIES} capabilities."),
            })
    return errors


def shot_attachment(*, timestamp=False, attachment_id=None) -> dict:
    value = normalize_attachment({
        "attachment_id": attachment_id,
        "kind": "shot",
        "config": {"timestamp": bool(timestamp)},
    })
    return value


def timestamp_attachment(*, attachment_id=None) -> dict:
    """Return the canonical standalone Time marker shape."""
    return normalize_attachment({"attachment_id": attachment_id,
                                 "kind": "timestamp",
                                 "config": {"standalone": True}})


def clone_for_split(channel_documents, attachments) -> tuple[dict, list]:
    """Clone applicable context for the right half of a section split.

    Authored Shot/Timestamp markers stay on the left. Other attachments receive
    fresh UI identities but retain emission_group_id so a combined render can
    deduplicate their semantic output.
    """
    clones, id_map = [], {}
    for attachment in normalize_attachments(attachments):
        if attachment["kind"] in {"shot", "timestamp"}:
            continue
        clone = copy.deepcopy(attachment)
        clone_id = _new_id()
        id_map[attachment["attachment_id"]] = clone_id
        clone["attachment_id"] = clone_id
        clones.append(clone)
    documents = {}
    for key, raw in (channel_documents or {}).items():
        document = normalize_prompt_document(raw)
        nodes = []
        for node in document["nodes"]:
            if node["type"] == "text":
                nodes.append({**node, "node_id": _new_id()})
            elif node["attachment_id"] in id_map:
                nodes.append({**node, "node_id": _new_id(),
                              "attachment_id": id_map[node["attachment_id"]]})
        documents[str(key)] = normalize_prompt_document({"nodes": nodes})
    return documents, clones


def normalize_disabled_capabilities(raw) -> list:
    """Ordered, de-duplicated capability ids a Reference tier turns off.

    Preserves ids the active Prompt Format does not declare: a project can hold
    one Reference used under several formats, and dropping an unrecognised id
    would silently re-enable that part on the format that owns it.
    """
    result = []
    for value in raw if isinstance(raw, list) else []:
        capability_id = str(value or "").strip()
        if capability_id and capability_id not in result:
            result.append(capability_id)
    return result


def normalize_semantic_unit(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    contributions = []
    seen = set()
    for value in raw.get("sources") or []:
        if not isinstance(value, dict):
            continue
        key = (str(value.get("entity_id") or ""),
               str(value.get("member_id") or ""))
        if not all(key) or key in seen:
            continue
        seen.add(key)
        contributions.append({
            "entity_id": key[0], "member_id": key[1],
            "contribution": str(value.get("contribution") or ""),
            "inherit_description": value.get("inherit_description") is True,
        })
    try:
        # Authored/API data reaches this normalizer outside any handler, so a
        # non-numeric order must not become a 500.
        order = int(raw.get("order") or 0)
    except (TypeError, ValueError):
        order = 0
    voice = raw.get("voice") if isinstance(raw.get("voice"), dict) else {}
    voice_member_id = str(voice.get("member_id") or "").strip() or None
    result = {
        "semantic_unit_id": str(raw.get("semantic_unit_id") or "").strip()
                            or _new_id(),
        "handle": normalize_prompt_handle(raw.get("handle")),
        "name": str(raw.get("name") or "Identity"),
        "kind": str(raw.get("kind") or "subject").strip() or "subject",
        "order": order,
        "sources": contributions,
        "definition": str(raw.get("definition") or ""),
        "attachment_defaults": copy.deepcopy(raw.get("attachment_defaults"))
                               if isinstance(raw.get("attachment_defaults"), dict)
                               else {},
        "voice": {"member_id": voice_member_id},
        # Prompt parts this identity does not contribute by default. Sparse:
        # an empty list is the ordinary "everything the format declares".
        "disabled_capabilities": normalize_disabled_capabilities(
            raw.get("disabled_capabilities")),
    }
    # Identity intent vocabulary belongs to the active Prompt Format. Keep the
    # authored value sparse and preserved—even when a future/other format owns
    # it—instead of synthesizing MiniMax defaults into every identity. The
    # renderer applies its existing semantic fallback only when the field is
    # actually needed.
    for field in ("visual_intent", "audio_intent"):
        if field in raw:
            value = str(raw.get(field) or "").strip()
            if value:
                result[field] = value
    return result


def _profile(profile_id, name, template_id, *, capabilities, writing_aids,
             separators=None, validators=None, role_catalogs=None,
             physical_populations=None, identity_kinds=None,
             contribution_catalog=None, speaker_policy=None) -> dict:
    value = {
        "profile_id": profile_id,
        "version": "1",
        "name": name,
        "template_id": template_id,
        "capabilities": capabilities,
        "writing_aids": writing_aids,
        "separators": separators or {"attachment": " ", "line": "\n"},
        "validators": validators or [],
        "role_catalogs": copy.deepcopy(role_catalogs or {}),
        "physical_populations": copy.deepcopy(physical_populations or []),
        "identity_kinds": copy.deepcopy(identity_kinds or []),
        "speaker_policy": copy.deepcopy(speaker_policy or {
            "enabled": False, "token_template": "",
            "compound_join": ",", "compound_order": "authored",
        }),
        "builtin": True,
    }
    if contribution_catalog is not None:
        value["contribution_catalog"] = copy.deepcopy(contribution_catalog)
    value["content_hash"] = content_hash(value)
    return value


_LANGUAGE_VALUES = [
    "English", "Spanish", "French", "German", "Italian", "Japanese",
    "Korean", "Chinese", "Portuguese", "Hindi",
]
_LANGUAGE_FIELD = {"language": {"type": "enum", "label": "Language",
                                "values": list(_LANGUAGE_VALUES)}}

# Shot framing, depth of field and field of view are ORDINARY cinematography
# vocabulary. Neither MiniMax guide tables them — both only use such phrases in
# prose examples — so unlike the camera-motion grammar below they are ours to
# define and are shared by every format rather than presented as a provider
# contract.
_FRAMING_AIDS = [
    {"id": "shot_distance", "label": "Shot distance",
     "text": "The shot is {distance}.",
     "fields": {"distance": {"type": "enum", "label": "Distance", "values": [
         {"value": "an extreme wide shot", "label": "Extreme wide"},
         {"value": "a wide shot", "label": "Wide"},
         {"value": "a medium-wide shot", "label": "Medium wide"},
         {"value": "a medium shot", "label": "Medium"},
         {"value": "a medium close-up", "label": "Medium close-up"},
         {"value": "a close-up", "label": "Close-up"},
         {"value": "an extreme close-up", "label": "Extreme close-up"},
     ]}}},
    {"id": "shot_composition", "label": "Shot composition",
     "text": "The camera frames the subject {composition}.",
     "fields": {"composition": {"type": "enum", "label": "Composition", "values": [
         {"value": "at eye level", "label": "Eye level"},
         {"value": "from a low angle", "label": "Low angle"},
         {"value": "from a high angle", "label": "High angle"},
         {"value": "from a bird's-eye view", "label": "Bird's eye"},
         {"value": "from a worm's-eye view", "label": "Worm's eye"},
         {"value": "over the shoulder", "label": "Over the shoulder"},
         {"value": "in a centred symmetrical frame", "label": "Centred"},
         {"value": "off-centre using the rule of thirds", "label": "Rule of thirds"},
         {"value": "in a Dutch-angled frame", "label": "Dutch angle"},
     ]}}},
    {"id": "depth_of_field", "label": "Depth of field",
     "text": "The image has {depth_of_field}{focus}.",
     "fields": {
         "depth_of_field": {"type": "enum", "label": "Depth of field", "values": [
             {"value": "a shallow depth of field", "label": "Shallow"},
             {"value": "a deep depth of field", "label": "Deep"},
         ]},
         "focus": {"type": "enum", "label": "Focus change", "optional": True,
                   "values": [
                       {"value": ", racking focus to the subject",
                        "label": "Rack focus to subject"},
                       {"value": ", racking focus to the background",
                        "label": "Rack focus to background"},
                   ]},
     }},
    {"id": "field_of_view", "label": "Field of view",
     "text": "The scene is captured on {lens}.",
     "fields": {"lens": {"type": "enum", "label": "Lens", "values": [
         {"value": "an ultra-wide fisheye lens", "label": "Fisheye"},
         {"value": "a wide-angle lens", "label": "Wide angle"},
         {"value": "a standard 50mm lens", "label": "Standard 50mm"},
         {"value": "a short telephoto portrait lens", "label": "Portrait telephoto"},
         {"value": "a long telephoto lens", "label": "Long telephoto"},
         {"value": "a macro lens", "label": "Macro"},
     ]}}},
]


def _generic_aids() -> list:
    return copy.deepcopy([
        {"id": "dialogue", "label": "Dialogue", "text": "<d>[{language}] {text}</d>",
         "fields": _LANGUAGE_FIELD},
        {"id": "voiceover", "label": "Voiceover", "text": "Voiceover: {text}"},
        {"id": "group_speech", "label": "Group speech", "text": "Group says: {text}"},
        {"id": "singing", "label": "Singing", "text": "Singing: {text}"},
        {"id": "scene_transition", "label": "Scene transition", "text": "<scenetrans>"},
        {"id": "cutoff", "label": "Cutoff", "text": "<cutoff>"},
        {"id": "visible_text", "label": "Visible text", "text": "\"{text}\""},
        {"id": "camera_motion", "label": "Camera motion", "text": "The camera {motion}.",
         "fields": {"motion": {"type": "enum", "label": "Motion", "values": [
             "pushes in", "pulls out", "pans left", "pans right", "tilts up",
             "tilts down", "trucks left", "trucks right", "orbits the subject",
             "remains static"]}}},
        *copy.deepcopy(_FRAMING_AIDS),
    ])


# MiniMax H3 §4.3 states the camera grammar as motion type + amplitude + speed,
# written as a natural action inside the shot rather than labels appended to a
# sentence, and says to add amplitude and speed only when they are meaningful —
# which is what makes those two fields `optional` rather than defaulted. The
# Generic ten-verb list this used to inherit expressed neither dimension and
# omitted half the documented motions.
_MINIMAX_CAMERA_MOTION = {
    "id": "camera_motion", "label": "Camera motion",
    "text": "The camera {motion} {amplitude} {speed}.",
    "fields": {
        "motion": {"type": "enum", "label": "Motion type", "values": [
            {"value": "zooms in", "label": "Zoom In"},
            {"value": "zooms out", "label": "Zoom Out"},
            {"value": "pushes in", "label": "Push In"},
            {"value": "pulls out", "label": "Pull Out"},
            {"value": "pans left", "label": "Pan Left"},
            {"value": "pans right", "label": "Pan Right"},
            {"value": "trucks left", "label": "Truck Left"},
            {"value": "trucks right", "label": "Truck Right"},
            {"value": "tilts up", "label": "Tilt Up"},
            {"value": "tilts down", "label": "Tilt Down"},
            {"value": "pedestals up", "label": "Pedestal Up"},
            {"value": "pedestals down", "label": "Pedestal Down"},
            {"value": "arcs around the subject", "label": "Arc Shot"},
            {"value": "tracks the subject", "label": "Tracking Shot"},
            {"value": "holds a static shot", "label": "Static Shot"},
            {"value": "shakes slightly", "label": "Shake Slightly"},
            {"value": "shakes strongly", "label": "Shake Strongly"},
            {"value": "takes the subject's point of view", "label": "POV"},
            {"value": "rolls clockwise", "label": "Roll Clockwise"},
            {"value": "rolls counterclockwise", "label": "Roll Counterclockwise"},
        ]},
        "amplitude": {"type": "enum", "label": "Amplitude", "optional": True,
                      "help": "Omitted for medium amplitude.", "values": [
                          {"value": "with small amplitude", "label": "Small"},
                          {"value": "with large amplitude", "label": "Large"},
                      ]},
        "speed": {"type": "enum", "label": "Speed", "optional": True,
                  "help": "Omitted for normal speed.", "values": [
                      {"value": "at slow speed", "label": "Slow"},
                      {"value": "at fast speed", "label": "Fast"},
                  ]},
    },
}


def _minimax_aids(description_channel: str) -> list:
    """MiniMax writing aids bound to the profile's own description channel.

    Base and Full Reference carry the SAME aids but different channel keys —
    `integrated_multimodal_description` versus `detailed_description` — so this
    cannot be one shared list. Every aid here is timeline description prose;
    none belongs in the soundscape, music, definition, summary or retention
    channels, which is what `channel_keys` says.
    """
    aids = _generic_aids()
    for aid in aids:
        aid["channel_keys"] = [description_channel]
        if aid["id"] == "voiceover":
            aid["text"] = ("The on-screen character says in an off-screen voiceover: "
                           "<d>[{language}] {text}</d> while the corresponding "
                           "on-screen character's lips remain completely closed.")
            aid["fields"] = copy.deepcopy(_LANGUAGE_FIELD)
        elif aid["id"] == "singing":
            # H3 sung lyrics use the same bounded-language <d> envelope as speech;
            # inheriting the Generic bare "Singing: {text}" emitted lyrics the model
            # reads as description rather than vocal content.
            aid["text"] = "Singing: <d>[{language}] {text}</d>"
            aid["fields"] = copy.deepcopy(_LANGUAGE_FIELD)
        elif aid["id"] == "camera_motion":
            aid.update(copy.deepcopy(_MINIMAX_CAMERA_MOTION))
            aid["channel_keys"] = [description_channel]
    return aids

BUILTIN_PROFILES = {
    "generic@1": _profile(
        "generic", "Generic", "standard",
        capabilities={
            "shot": {"placement": "section_prefix"},
            "timestamp": {"placement": "section_prefix"},
            "prompt_link": {"placement": "inline"},
            "prompt_link_scope": {"placement": "section_prefix"},
            "reference": {
                "placement": "inline",
                "derived": {
                    "derived_prompt": {
                        "order": 1,
                        "channel_key": "visual",
                        "placement": "inline",
                        "label": "Reference prompt",
                        "description": "Prompt text staged by a physical Reference.",
                        "example": "A weathered brass compass.",
                        "help": "Emits the staged Reference prompt at the attachment anchor.",
                        "fields": {},
                    },
                },
            },
            "vocal_event": {"placement": "inline"},
        }, writing_aids=_generic_aids(),
        speaker_policy=DEFAULT_SPEAKER_POLICY),
    "minimax_h3_base@1": _profile(
        "minimax_h3_base", "MiniMax H3 Base", "minimax_h3_base",
        capabilities={
            "shot": {"channel_key": "integrated_multimodal_description",
                     "placement": "section_prefix", "token_kind": "shot"},
            "timestamp": {"channel_key": "integrated_multimodal_description",
                          "placement": "section_prefix"},
            "prompt_link": {"placement": "inline"},
            "prompt_link_scope": {"placement": "section_prefix"},
            "vocal_event": {"channel_key": "integrated_multimodal_description",
                            "placement": "inline"},
        }, writing_aids=_minimax_aids("integrated_multimodal_description"),
        validators=["minimax_base_setup", "managed_speakers"],
        identity_kinds=[{**MINIMAX_SUBJECT_KIND,
                         "referenced_label_template": ""}],
        speaker_policy=DEFAULT_SPEAKER_POLICY),
    "minimax_h3_ref@1": _profile(
        "minimax_h3_ref", "MiniMax H3 Full Reference", "minimax_h3_ref",
        capabilities={
            "shot": {"channel_key": "detailed_description",
                     "placement": "section_prefix", "token_kind": "shot"},
            "timestamp": {"channel_key": "detailed_description",
                          "placement": "section_prefix"},
            "prompt_link": {"placement": "inline"},
            "prompt_link_scope": {"placement": "section_prefix"},
            "reference": {
                "derived": {
                    "definitions": {
                        "order": 1, "channel_key": "subject_definitions",
                        "placement": "section_prefix", "label": "Definition",
                        # ref guide §2 "Give each item its own line".
                        "separator": "\n",
                        "description": "Defines semantic identities from authored prose and attributed References.",
                        "example": "<Subject 1> is a middle-aged man with a robust build.",
                        "help": "Use identity prose for description-only subjects or inherit it from selected physical References.",
                        "fields": {},
                    },
                    "summary": {
                        "order": 2, "channel_key": "summary",
                        "placement": "section_prefix", "label": "Summary",
                        "description": "States the MiniMax H3 Full Reference task and optional summary prose.",
                        "example": "[reference generation] The subject crosses the corridor.",
                        "help": "Task types are MiniMax H3 Full Reference syntax owned by this Prompt Format.",
                        "fields": {
                            "task_types": {
                                "type": "enum_multi",
                                "values": copy.deepcopy(MINIMAX_TASK_TYPE_CHOICES),
                                "label": "Summary task types",
                                "help": "Choose the H3 reference operations represented by staged roles.",
                                "example": "reference generation + audio reference",
                                "default_source": "roles",
                            },
                        },
                    },
                    "retention": {
                        "order": 3, "channel_key": "retention_analysis",
                        "placement": "section_prefix", "label": "Retention",
                        # ref guide §4 "Use one line for each reference label".
                        "separator": "\n",
                        "description": "Describes which visual and audio characteristics should be retained.",
                        "example": "<Subject 1> is fully preserved.",
                        "help": "Defaults may be refined on an identity and overridden on an attachment.",
                        "fields": {
                            "visual_intent": {
                                "type": "enum", "values": list(copy.deepcopy(VISUAL_INTENT_CHOICES)),
                                "label": "Visual handling",
                                "help": "How strongly the physical visual identity should be retained.",
                                "example": "Fully preserve",
                            },
                            "audio_intent": {
                                "type": "enum", "values": list(copy.deepcopy(AUDIO_INTENT_CHOICES)),
                                "label": "Audio handling",
                                "help": "How the attached voice or audio should influence generation.",
                                "example": "Reference audio characteristics",
                            },
                        },
                    },
                    "mentions": {
                        "order": 4, "channel_key": "detailed_description",
                        "placement": "inline", "label": "Scene mention",
                        "description": "Places resolved Reference and identity tokens in scene prose.",
                        "example": "<Subject 1> picks up <Picture 2>.",
                        "help": "The editor resolves user handles to this format's canonical tokens.",
                        "fields": {},
                    },
                    "audio_relationship": {
                        "order": 5, "channel_key": "summary",
                        "placement": "section_prefix", "label": "Audio relationship",
                        "description": "Relates an audio Reference to its semantic identity.",
                        "example": "<Audio 1> is the voice reference for <Subject 1>.",
                        "help": "Use this when audio is staged as part of an identity relationship.",
                        "fields": {},
                    },
                },
            },
            "vocal_event": {"channel_key": "detailed_description",
                            "placement": "inline"},
        }, writing_aids=_minimax_aids("detailed_description"),
        validators=["minimax_reference_setup", "managed_speakers"],
        role_catalogs=MINIMAX_H3_ROLE_CATALOGS,
        physical_populations=MINIMAX_H3_PHYSICAL_POPULATIONS,
        identity_kinds=[MINIMAX_SUBJECT_KIND],
        speaker_policy=DEFAULT_SPEAKER_POLICY),
}


def supported_provider_versions(custom_profiles=None) -> dict[str, frozenset[str]]:
    result = defaultdict(set)
    for profile in [*BUILTIN_PROFILES.values(), *(custom_profiles or [])]:
        if not isinstance(profile, dict):
            continue
        provider_id = str(profile.get("profile_id") or "")
        version = str(profile.get("version") or "")
        if provider_id and version:
            result[provider_id].add(version)
    return {key: frozenset(values) for key, values in result.items()}

PROFILE_DEFINITION_FIELDS = (
    "template_id", "compatible_templates", "capabilities", "writing_aids",
    "separators", "validators", "role_catalogs", "physical_populations",
    "identity_kinds", "contribution_catalog", "speaker_policy",
)
PROFILE_RESERVED_DEFINITION_FIELDS = {
    "profile_id", "version", "name", "builtin", "content_hash", "fork_seed",
}


def profile_fork_seed(profile) -> dict:
    """Return only the editable definition owned by a profile fork.

    Identity and server-derived fields are deliberately excluded.  This is the
    sole profile definition published to authoring clients, so a built-in can be
    forked without teaching the browser a second canonical profile registry.
    """
    source = profile if isinstance(profile, dict) else {}
    return {
        key: copy.deepcopy(source[key])
        for key in PROFILE_DEFINITION_FIELDS
        if key in source
    }


TEMPLATE_DEFAULT_PROFILES = {
    "standard": "generic@1", "sonder": "generic@1",
    "minimax_h3_base": "minimax_h3_base@1",
    "minimax_h3_ref": "minimax_h3_ref@1",
}


def normalize_profile(raw, *, builtin=False) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("invalid_profile")
    serialized = _canonical_json(raw).encode("utf-8")
    if len(serialized) > PROFILE_LIMIT_BYTES:
        raise ValueError("profile_too_large")
    capabilities = raw.get("capabilities")
    if not isinstance(capabilities, dict) or len(capabilities) > MAX_CAPABILITIES:
        raise ValueError("invalid_profile_capabilities")

    def assert_declarative(value, depth=0):
        # Derived capability metadata nests field choices below the existing
        # capability object. Sixteen keeps the declaration bounded while
        # leaving deliberate headroom for value-label-description entries.
        if depth > PROFILE_DECLARATION_MAX_DEPTH:
            raise ValueError("profile_recursion_limit")
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                if lowered in {"regex", "regexp", "expression", "script", "html",
                               "javascript", "python", "callback", "function"}:
                    raise ValueError("executable_profile_field_forbidden")
                assert_declarative(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                assert_declarative(child, depth + 1)
        elif isinstance(value, str):
            # Presentation-only help and examples use the same conservative
            # string screen as formatter text. Custom authors therefore cannot
            # use these executable-looking sequences in prose.
            lowered = value.lower()
            if any(token in lowered for token in (
                    "<script", "<iframe", "javascript:", "onerror=", "onclick=",
                    "{{", "{%", "${", "eval(", "function(", "=>")):
                raise ValueError("executable_formatter_forbidden")

    assert_declarative(capabilities)
    assert_declarative(raw.get("separators") or {})
    assert_declarative(raw.get("validators") or [])
    assert_declarative(raw.get("role_catalogs") or {})
    assert_declarative(raw.get("physical_populations") or [])
    assert_declarative(raw.get("identity_kinds") or [])
    assert_declarative(raw.get("contribution_catalog") or {})
    assert_declarative(raw.get("speaker_policy") or {})

    def validate_field_declaration(field, *, owner):
        problems = []
        _validate_declared_field(
            field, field=owner,
            add=lambda code, message, path="": problems.append(
                (code, message, path)))
        if not problems:
            return
        if (not isinstance(field, dict)
                or field.get("type") not in FIELD_DECLARATION_TYPES):
            raise ValueError(f"{owner}_fields_must_be_enums")
        raise ValueError(f"invalid_{owner}_enum")
    role_catalogs = _normalized_role_catalog(raw.get("role_catalogs"))
    if raw.get("role_catalogs") and not role_catalogs:
        raise ValueError("invalid_profile_role_catalogs")
    separators = raw.get("separators")
    if separators is not None and not isinstance(separators, dict):
        raise ValueError("invalid_profile_separators")
    separators = separators if isinstance(separators, dict) else {}
    for separator_name, separator_value in separators.items():
        if str(separator_name) not in SEPARATOR_NAMES:
            raise ValueError("unknown_profile_separator")
        # A non-string separator reaches `str.join` at compile time and would
        # raise AttributeError deep inside emission assembly, i.e. a 500.
        if not isinstance(separator_value, str) or len(separator_value) > 16:
            raise ValueError("invalid_profile_separator")
    for capability_kind, declaration in capabilities.items():
        if str(capability_kind) not in SUPPORTED_ATTACHMENT_KINDS:
            raise ValueError("unknown_profile_capability_kind")
        # A non-object declaration reaches `.get` in routing/default-capability
        # resolution.  Refuse it here rather than crashing compilation.
        if not isinstance(declaration, dict):
            raise ValueError("invalid_profile_capability")
        placement = declaration.get("placement")
        if placement is not None and placement not in PLACEMENT_PHASES:
            raise ValueError("invalid_capability_placement")
        channel_key = declaration.get("channel_key")
        if channel_key is not None and (not isinstance(channel_key, str)
                                        or len(channel_key) > 128):
            raise ValueError("invalid_capability_channel_key")
        routes = declaration.get("routes")
        if routes is not None:
            if not isinstance(routes, dict) or len(routes) > MAX_CAPABILITIES:
                raise ValueError("invalid_capability_routes")
            for route_name, route_value in routes.items():
                if (not isinstance(route_name, str) or not route_name.strip()
                        or not isinstance(route_value, str)
                        or not route_value.strip() or len(route_value) > 128):
                    raise ValueError("invalid_capability_routes")
        raw_formatter = declaration.get("formatter")
        if raw_formatter is not None and not isinstance(raw_formatter, str):
            raise ValueError("invalid_capability_formatter")
        fields_declaration = declaration.get("fields")
        if fields_declaration is not None and not isinstance(fields_declaration, dict):
            raise ValueError("invalid_capability_fields")
        formatter = str(raw_formatter or "")
        if not formatter:
            continue
        if len(formatter.encode("utf-8")) > FORMATTER_LIMIT_BYTES:
            raise ValueError("formatter_too_large")
        field_declarations = (declaration.get("fields")
                              if isinstance(declaration.get("fields"), dict)
                              else {})
        for field_name, field in field_declarations.items():
            validate_field_declaration(field, owner="capability")
        substitutions = set(re.findall(
            r"\{([A-Za-z_][A-Za-z0-9_]*)\}", formatter))
        if not substitutions.issubset(
                set(field_declarations) | {"text", "value"}):
            raise ValueError("unknown_formatter_substitution")
    aids = []
    for aid in raw.get("writing_aids") or []:
        if not isinstance(aid, dict):
            raise ValueError("invalid_writing_aid")
        text = str(aid.get("text") or "")
        if len(text.encode("utf-8")) > FORMATTER_LIMIT_BYTES:
            raise ValueError("formatter_too_large")
        assert_declarative(aid)
        fields = aid.get("fields") if isinstance(aid.get("fields"), dict) else {}
        for field_name, declaration in fields.items():
            validate_field_declaration(declaration, owner="writing_aid")
        allowed_substitutions = set(fields) | {"text"}
        substitutions = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", text))
        if not substitutions.issubset(allowed_substitutions):
            raise ValueError("unknown_formatter_substitution")
        # Which channels an aid belongs to. Absent or empty means every channel,
        # so an aid that predates this key keeps its current reach. Normalization
        # stays tolerant and only screens shape; whether the keys name channels
        # the bound template actually has is a declaration-level question,
        # reported by `profile_declaration_errors` rather than raised at rest.
        channel_keys = aid.get("channel_keys")
        if channel_keys is not None:
            if (not isinstance(channel_keys, list)
                    or len(channel_keys) > MAX_CAPABILITIES
                    or not all(isinstance(key, str) and 0 < len(key) <= 128
                               for key in channel_keys)):
                raise ValueError("invalid_writing_aid_channels")
        aids.append(copy.deepcopy(aid))
    validators = []
    for validator in raw.get("validators") or []:
        if isinstance(validator, str):
            if validator not in {"minimax_base_setup", "minimax_reference_setup",
                                  "managed_speakers"}:
                raise ValueError("unknown_profile_validator")
            validators.append(validator)
            continue
        if not isinstance(validator, dict):
            raise ValueError("invalid_profile_validator")
        kind = str(validator.get("kind") or "")
        if kind not in {"required_channel", "max_channel_chars",
                        "max_final_chars", "require_attachment_kind"}:
            raise ValueError("unknown_profile_validator")
        severity = str(validator.get("severity") or "error")
        if severity not in {"error", "warning"}:
            raise ValueError("invalid_profile_validator_severity")
        normalized_validator = {"kind": kind, "severity": severity,
                                "message": str(validator.get("message") or "")[:512]}
        if kind in {"required_channel", "max_channel_chars"}:
            channel_key = str(validator.get("channel_key") or "").strip()
            if not channel_key:
                raise ValueError("profile_validator_channel_required")
            normalized_validator["channel_key"] = channel_key
        if kind in {"max_channel_chars", "max_final_chars"}:
            try:
                limit = int(validator.get("limit"))
            except (TypeError, ValueError):
                raise ValueError("profile_validator_limit_required") from None
            if not 1 <= limit <= MAX_COMPILED_PROMPT:
                raise ValueError("profile_validator_limit_out_of_bounds")
            normalized_validator["limit"] = limit
        if kind == "require_attachment_kind":
            attachment_kind = str(validator.get("attachment_kind") or "")
            if attachment_kind not in SUPPORTED_ATTACHMENT_KINDS:
                raise ValueError("profile_validator_attachment_kind_invalid")
            normalized_validator["attachment_kind"] = attachment_kind
        validators.append(normalized_validator)
    # An explicit multi-template declaration is the only way to author a format
    # that several channel templates may select; without preserving it here the
    # field read by `profile_compatible_templates` could never exist.
    raw_templates = raw.get("compatible_templates")
    compatible_templates = []
    if raw_templates is not None:
        if not isinstance(raw_templates, list) or len(raw_templates) > 64:
            raise ValueError("invalid_profile_compatible_templates")
        for entry in raw_templates:
            if not isinstance(entry, str) or not entry.strip() or len(entry) > 128:
                raise ValueError("invalid_profile_compatible_templates")
            if entry not in compatible_templates:
                compatible_templates.append(entry)
    value = {
        "profile_id": str(raw.get("profile_id") or "custom").strip(),
        "version": str(raw.get("version") or "1").strip(),
        "name": str(raw.get("name") or "Custom profile"),
        "template_id": str(raw.get("template_id") or "standard"),
        **({"compatible_templates": compatible_templates}
           if compatible_templates else {}),
        "capabilities": copy.deepcopy(capabilities),
        "writing_aids": aids,
        "separators": copy.deepcopy(separators),
        "validators": validators,
        "role_catalogs": role_catalogs,
        "physical_populations": copy.deepcopy(
            raw.get("physical_populations")
            if isinstance(raw.get("physical_populations"), list) else
            raw.get("physical_populations", [])),
        "identity_kinds": copy.deepcopy(
            raw.get("identity_kinds")
            if isinstance(raw.get("identity_kinds"), list) else
            raw.get("identity_kinds", [])),
        "speaker_policy": copy.deepcopy(
            raw.get("speaker_policy") if "speaker_policy" in raw else {
                "enabled": False, "token_template": "",
                "compound_join": ",", "compound_order": "authored",
            }),
        "builtin": bool(builtin),
    }
    if "contribution_catalog" in raw:
        value["contribution_catalog"] = copy.deepcopy(
            raw.get("contribution_catalog"))
    value["content_hash"] = content_hash(value)
    return value


UNIVERSAL_PROFILE_TEMPLATE = "*"


class ProfileResolutionError(ValueError):
    """A prompt format that cannot be used, with a stable diagnostic code.

    A distinct type so compile call sites can convert *this* into a controlled
    authoring diagnostic without also swallowing genuine internal ValueErrors
    and reporting them under a nonsense code.
    """

    def __init__(self, code, detail=""):
        self.code = str(code)
        self.detail = str(detail)
        super().__init__(f"{self.code}:{self.detail}" if self.detail else self.code)


def profile_key(profile) -> str:
    return f"{(profile or {}).get('profile_id')}@{(profile or {}).get('version')}"


def profile_compatible_templates(profile) -> set:
    """Channel templates a prompt format may be selected under.

    Provider requirements are enforced per channel template — MiniMax H3
    validation branches on the template id — so a Standard template paired with
    `minimax_h3_ref@1` would claim the format while skipping every MiniMax
    requirement.  A format declaring the neutral `standard` base stays
    universal, which is what generic and project-custom templates rely on;
    an explicit `compatible_templates` list names several.
    """
    declared = (profile or {}).get("compatible_templates")
    if isinstance(declared, list) and declared:
        return {str(value) for value in declared}
    template_id = str((profile or {}).get("template_id") or "")
    if template_id in {"", "standard"}:
        return {UNIVERSAL_PROFILE_TEMPLATE}
    return {template_id}


def profile_matches_template(profile, template_id, *, template=None) -> bool:
    allowed = profile_compatible_templates(profile)
    if (UNIVERSAL_PROFILE_TEMPLATE in allowed
            or str(template_id or "") in allowed):
        return True
    # A channel template naming this format as its own default HAS declared the
    # pairing. Copying a MiniMax template mints a new custom id, so without this
    # every scene inheriting that copy's default would be permanently blocked by
    # a format the template itself selected.
    declared = str((template or {}).get("default_context_profile") or "")
    if not declared:
        return False
    return declared in {profile_key(profile),
                        str((profile or {}).get("profile_id") or "")}


def _require_profile_template(profile, resolved_template) -> dict:
    template_id = str((resolved_template or {}).get("id") or "")
    if not profile_matches_template(profile, template_id,
                                    template=resolved_template):
        raise ProfileResolutionError("profile_template_incompatible",
                                     f"{profile_key(profile)}:{template_id}")
    return profile


def _require_profile_declarations(profile, *, template=None) -> dict:
    declaration_errors = profile_declaration_errors(profile, template=template)
    if declaration_errors:
        first = declaration_errors[0]
        raise ProfileResolutionError(
            "invalid_profile_declaration",
            f"{first.get('code', 'invalid')}:{first.get('field', '')}")
    return profile


def resolve_profile(profile=None, *, template=None, custom_profiles=None) -> dict:
    resolved_template = prompt_channel_templates.get_channel_template(template)
    requested = profile
    if isinstance(profile, dict):
        normalized = _require_profile_template(
            normalize_profile(profile), resolved_template)
        return _require_profile_declarations(
            normalized, template=resolved_template)
    if not requested:
        requested = (resolved_template.get("default_context_profile")
                     or TEMPLATE_DEFAULT_PROFILES.get(
                         resolved_template.get("id"), "generic@1"))
    key = str(requested)
    if "@" not in key:
        key = f"{key}@1"
    if key in BUILTIN_PROFILES:
        normalized = _require_profile_template(
            copy.deepcopy(BUILTIN_PROFILES[key]), resolved_template)
        return _require_profile_declarations(
            normalized, template=resolved_template)
    for value in custom_profiles or []:
        try:
            normalized = normalize_profile(value)
        except ValueError:
            continue
        candidate = f"{normalized['profile_id']}@{normalized['version']}"
        if candidate == key:
            normalized = _require_profile_template(normalized, resolved_template)
            return _require_profile_declarations(
                normalized, template=resolved_template)
    raise ProfileResolutionError("unknown_profile", key)


def _attachment_map(attachments):
    return {value["attachment_id"]: value
            for value in normalize_attachments(attachments)}


def _join_emissions(parts, separator=" ") -> str:
    return separator.join(str(value or "").strip() for value in parts
                          if str(value or "").strip()).strip()


def declared_capability_separator(capability, profile, default=" ") -> str:
    """The separator that PRECEDES this capability's emission.

    Declared, never inferred from the text. MiniMax `definitions` and
    `retention` emit one line per reference label and the ref guide requires
    that shape in both §2 and §4, so those declarations carry `"\\n"`; two chips
    naming different Subjects therefore keep a line each instead of being
    space-joined into one run (PR-15). Everything else falls back to the
    profile separator, which is what inline prose wants — a mention inside a
    sentence must not start a new line.

    Expiry: this reads a per-capability override on top of the profile-wide
    `separators.attachment`. It can collapse back into the profile value only if
    every declared capability in every format wants the same joiner, which the
    guide's own line rules make unlikely.
    """
    kind = str(capability.get("kind") or capability.get("capability_id") or "")
    declaration = _reference_derived_view(profile).get(kind)
    separator = (declaration or {}).get("separator") \
        if isinstance(declaration, dict) else None
    return separator if isinstance(separator, str) and separator else default


def _join_declared_emissions(parts, default=" ") -> str:
    """Join `(text, separator)` pairs, each separator preceding its own part.

    The first surviving part never emits a separator, so a leading line-joined
    capability does not open the channel with a blank line.
    """
    result = ""
    for text, separator in parts:
        value = str(text or "").strip()
        if not value:
            continue
        result = value if not result else f"{result}{separator or default}{value}"
    return result.strip()


def _speaker_bindings(events):
    order = {}
    next_number = 1
    for event in events:
        subjects = event["attachment"]["source"].get("subject_ids") or []
        voice_id = str(event["attachment"]["source"].get("voice_id") or "")
        keys = [str(value) for value in subjects if str(value)]
        if not keys and voice_id:
            keys = [f"voice:{voice_id}"]
        if not keys:
            keys = [f"event:{event['attachment']['attachment_id']}"]
        for key in keys:
            if key not in order:
                order[key] = next_number
                next_number += 1
        event["speaker_numbers"] = [order[key] for key in keys]
    return order


def _render_vocal_event(attachment, speaker_numbers, speaker_policy=None):
    config = attachment["config"]
    event_type = str(config.get("event_type") or "dialogue")
    if event_type not in VOCAL_EVENT_TYPES:
        event_type = "dialogue"
    text = str(config.get("text") or "").strip()
    language = str(config.get("language") or "English").strip() or "English"
    policy = speaker_policy if isinstance(speaker_policy, dict) else {}
    speaker_tokens = [declared_label(policy, value, "token_template")
                      for value in speaker_numbers]
    speaker_tokens = [value for value in speaker_tokens if value]
    joiner = str(policy.get("compound_join") or ",")
    if len(speaker_tokens) > 1:
        # The format declares its intended compound order, but PR-16 deliberately
        # remains the tracked behavior until its focused fix; preserve authored
        # selection order here so this declaration migration is byte-identical.
        speaker_token = joiner.join(
            value[1:-1] if value.startswith("(") and value.endswith(")") else value
            for value in speaker_tokens)
        first = speaker_tokens[0]
        speaker_token = (f"({speaker_token})" if first.startswith("(")
                         and first.endswith(")") else speaker_token)
    else:
        speaker_token = speaker_tokens[0] if speaker_tokens else ""
    subject_phrase = str(config.get("subject_phrase") or "").strip()
    prefix = " ".join(value for value in (subject_phrase, speaker_token) if value)
    if event_type == "dialogue":
        return f"{prefix} says: <d>[{language}] {text}</d>".strip()
    if event_type == "group_speech":
        return f"{prefix} say together: <d>[{language}] {text}</d>".strip()
    if event_type == "singing":
        return f"{prefix} sings: <d>[{language}] {text}</d>".strip()
    if event_type == "voiceover":
        return (f"{prefix} says in an off-screen voiceover: "
                f"<d>[{language}] {text}</d> while the corresponding on-screen "
                "character's lips remain completely closed.").strip()
    return f"{prefix} narrates: <d>[{language}] {text}</d>".strip()


def _reference_labels(attachment, context):
    labels = []
    manifest = context.get("ordinal_manifest") or {}
    profile = context.get("profile") or {}
    units = context.get("semantic_units_by_id") or {}
    audio_prefixes = [
        declared_label_prefix(declaration)
        for declaration in physical_population_declarations(profile)
        if str(declaration.get("token_kind") or "") == "audio"
        and declared_label_prefix(declaration)
    ]
    for source_id in attachment["source"].get("semantic_unit_ids") or []:
        unit = units.get(str(source_id)) or {}
        kind = str(unit.get("kind") or "subject")
        declaration = identity_kind_for(profile, kind)
        number = (manifest.get(identity_ordinal_key(kind)) or {}).get(str(source_id))
        label_field = ("assetless_label_template" if not unit.get("sources")
                       else "referenced_label_template")
        label = declared_label(declaration, number, label_field)
        if label:
            labels.append(label)
            for label in context.get("unit_source_labels", {}).get(str(source_id)) or []:
                if (any(str(label).startswith(prefix) for prefix in audio_prefixes)
                        and label not in labels):
                    labels.append(str(label))
    for declaration in physical_population_declarations(profile):
        source_key = str(declaration.get("source_key") or "")
        ordinal_key = str(declaration.get("ordinal_key") or "")
        for source_id in attachment["source"].get(source_key) or []:
            number = (manifest.get(ordinal_key) or {}).get(str(source_id))
            label = declared_label(declaration, number)
            if label:
                labels.append(label)
    return labels


def _minimax_task_types(context, configured=(), profile=None):
    """Return guide-ordered, explicit-role-derived H3 task types.

    Physical media presence never implies editing, continuation, reuse, or
    reference. An explicit chip selection overrides the staged-role default.
    """
    resolved_profile = profile or context.get("profile") or {}
    field = declared_reference_field(resolved_profile, "summary", "task_types")
    declared = [choice["value"] for choice in declared_field_choices(field)]
    explicit = {str(raw or "").strip().lower() for raw in configured or []}
    explicit.intersection_update(declared)
    if explicit:
        return [value for value in declared if value in explicit]
    if field.get("default_source") != "roles":
        return []
    found = set()
    manifest = context.get("setup_manifest") or {}
    declarations = physical_population_declarations(resolved_profile)
    rows_by_token = {
        str(declaration.get("token_kind") or ""): manifest.get(
            str(declaration.get("key") or "")) or []
        for declaration in declarations if isinstance(declaration, dict)
    }
    for row in rows_by_token.get("picture", []):
        role = str(row.get("role") or "").strip().lower().replace("-", "_")
        if role in {"first_frame", "last_frame", "keyframe", "edited_keyframe",
                    "composition_anchor"}:
            found.add("keyframe completion")
        elif role in {"storyboard", "identity", "environment", "style", "motion",
                      "reference_generation"}:
            found.add("reference generation")
    for row in rows_by_token.get("video", []):
        role = str(row.get("role") or "").strip().lower().replace("-", "_")
        if role == "video_editing":
            found.add("video editing")
        elif role == "video_continuation":
            found.add("video continuation")
        elif role in {"temporal_structure", "motion", "camera", "rhythm",
                      "reference_generation"}:
            found.add("reference generation")
    for row in rows_by_token.get("audio", []):
        role = str(row.get("role") or "").strip().lower()
        if role == "audio_reuse":
            found.add("audio reuse")
        elif role in {"audio_reference", "timbre", "rhythm", "sound_texture"}:
            found.add("audio reference")
    return [value for value in declared if value in found]


def _definition_before_source(definition: str, source: str) -> str:
    """Drop a definition's terminal period when a source citation follows it.

    A definition inherited from Library member prose normally ends in a full
    stop, and the citation is appended after it — producing a fragment the guide
    never writes: `...combat boots. from <Picture 1>`.  The guide weaves the
    citation into the sentence instead.

    Only `.` is dropped.  A definition ending in `?` or `!` keeps it, because
    removing those changes the meaning rather than the punctuation
    ("Is she the one?" must not become "Is she the one from <Picture 1>").
    """
    value = str(definition or "").rstrip()
    if not source or not value.endswith("."):
        return value
    return value[:-1].rstrip()


def _subject_definition(config, unit, context) -> tuple[str, str]:
    """Resolve authored Subject prose and its inheritance source."""
    configured = str(config.get("definition") or "").strip()
    if configured:
        return configured, "chip"
    unit_definition = str(unit.get("definition") or "").strip()
    if unit_definition:
        return unit_definition, "subject"
    member_ids = {str(value.get("member_id") or "")
                  for value in unit.get("sources") or []
                  if isinstance(value, dict)}
    prompts = []
    for row in (context.get("setup_manifest", {}).get("presentation") or []):
        member_id = str(row.get("member_id") or row.get("video_member_id") or "")
        prompt = str(row.get("member_prompt") or "").strip()
        if member_id in member_ids and prompt and prompt not in prompts:
            prompts.append(prompt)
    return "; ".join(prompts), ("member" if prompts else "")


def _members_by_id(context) -> dict:
    """Lazy member lookup, cached on the context.

    Built on demand rather than beside `references_by_id`, which is assigned
    part-way through compilation: this runs from the emission path and must not
    depend on having been reached after that assignment.
    """
    cached = context.get("_members_by_id")
    if isinstance(cached, dict):
        return cached
    result = {}
    for reference in context.get("references") or []:
        if not isinstance(reference, dict):
            continue
        for member in reference.get("members") or []:
            member_id = str(member.get("member_id") or "") if isinstance(
                member, dict) else ""
            if member_id:
                result[member_id] = member
    context["_members_by_id"] = result
    return result


def _attachment_member_ids(attachment, context) -> list[str]:
    """Physical members this attachment draws on, directly or via identities.

    An identity contributes its own sources so a member default sits BELOW the
    identity that draws from it, matching how `_subject_definition` already
    falls back from identity prose to contributing member prose.
    """
    source = attachment.get("source") or {}
    profile = context.get("profile") or {}
    result = []
    for declaration in physical_population_declarations(profile):
        source_key = str(declaration.get("source_key") or "")
        if not source_key:
            continue
        for source_id in source.get(source_key) or []:
            value = str(source_id or "")
            if value and value not in result:
                result.append(value)
    units = context.get("semantic_units_by_id") or {}
    for unit_id in source.get("semantic_unit_ids") or []:
        unit = units.get(str(unit_id)) or {}
        for contribution in unit.get("sources") or []:
            value = str((contribution or {}).get("member_id") or "")
            if value and value not in result:
                result.append(value)
    return result


def _inherited_capability_enabled(attachment, capability, context) -> bool:
    """Shared default for a capability the chip does not state an opinion on.

    Whether a Reference contributes a prompt part at all is a property of the
    Reference, not of one section, so it is authored once on the member or the
    identity and listed in `disabled_capabilities`. A chip that DOES state
    `enabled` keeps its own value: that is per-section suppression and stays
    local, exactly as before.

    Identity beats member, matching the field precedence chain. A member is
    consulted only when every selected member agrees, so two members disagreeing
    fall through to enabled rather than one silently winning.
    """
    capability_id = str(capability.get("capability_id")
                        or capability.get("kind") or "")
    if not capability_id:
        return True
    units = context.get("semantic_units_by_id") or {}
    selected_units = [units.get(str(unit_id)) for unit_id
                      in (attachment.get("source") or {}).get(
                          "semantic_unit_ids") or []]
    selected_units = [unit for unit in selected_units if unit]
    if selected_units:
        opinions = [capability_id in (unit.get("disabled_capabilities") or [])
                    for unit in selected_units]
        if all(opinions):
            return False
        if not any(opinions):
            return True
        return True
    members_by_id = _members_by_id(context)
    selected_members = [members_by_id[member_id] for member_id
                        in _attachment_member_ids(attachment, context)
                        if member_id in members_by_id]
    if selected_members:
        opinions = [capability_id in (member.get("disabled_capabilities") or [])
                    for member in selected_members]
        if opinions and all(opinions):
            return False
    return True


def _common_member_attachment_defaults(attachment, context) -> dict:
    """Defaults shared by every physical member this attachment draws on.

    Same all-selected-sources-agree rule as the identity tier below: a field
    only inherits when every selected member states it and they agree, so two
    members with conflicting defaults fall through rather than one silently
    winning by ordering.
    """
    members_by_id = _members_by_id(context)
    selected = [members_by_id[member_id] for member_id
                in _attachment_member_ids(attachment, context)
                if member_id in members_by_id]
    if not selected:
        return {}
    defaults = [member.get("attachment_defaults")
                if isinstance(member.get("attachment_defaults"), dict) else {}
                for member in selected]
    result = {}
    for field in REFERENCE_OVERRIDE_FIELDS:
        values = [value[field] for value in defaults if field in value]
        if (values and len(values) == len(defaults)
                and all(value == values[0] for value in values[1:])):
            result[field] = copy.deepcopy(values[0])
    return result


def _common_identity_attachment_defaults(attachment, context) -> dict:
    units = context.get("semantic_units_by_id") or {}
    selected = [units.get(str(unit_id)) or {} for unit_id in
                (attachment.get("source") or {}).get("semantic_unit_ids") or []]
    selected = [unit for unit in selected if unit]
    if not selected:
        return {}
    defaults = [unit.get("attachment_defaults")
                if isinstance(unit.get("attachment_defaults"), dict) else {}
                for unit in selected]
    result = {}
    for field in REFERENCE_OVERRIDE_FIELDS:
        values = [value[field] for value in defaults if field in value]
        if (values and len(values) == len(defaults)
                and all(value == values[0] for value in values[1:])):
            result[field] = copy.deepcopy(values[0])
    return result


def effective_reference_config(attachment, capability, context) -> dict:
    """Resolve Reference config through its one sparse precedence chain.

    Format defaults < common physical member attachment defaults < common
    identity attachment defaults < non-inheritable attachment config < chip
    overrides < capability config. Capability config is sparse and
    intentionally wins for capability-owned fields.

    The member tier is what makes "the attachment follows current
    Reference/Identity defaults; edit the chip only for sparse deviations"
    true for a physical Reference. Without it only prompt text and the two
    intents inherited, so every other field was a per-chip deviation.
    """
    profile = context.get("profile") or {}
    declaration = (profile.get("capabilities") or {}).get("reference") or {}
    kind = str((capability or {}).get("kind")
               or (capability or {}).get("capability_id") or "")
    config = (copy.deepcopy(declaration.get("defaults"))
              if isinstance(declaration.get("defaults"), dict) else {})
    by_capability = declaration.get("capability_defaults")
    if isinstance(by_capability, dict) and isinstance(by_capability.get(kind), dict):
        config.update(copy.deepcopy(by_capability[kind]))
    config.update(_common_member_attachment_defaults(attachment, context))
    config.update(_common_identity_attachment_defaults(attachment, context))
    attachment_config = (attachment.get("config")
                         if isinstance(attachment.get("config"), dict) else {})
    config.update({key: copy.deepcopy(value)
                   for key, value in attachment_config.items()
                   if key != "overrides"})
    overrides = attachment_config.get("overrides")
    if isinstance(overrides, dict):
        config.update(copy.deepcopy(overrides))
    capability_config = (capability or {}).get("config")
    if isinstance(capability_config, dict):
        config.update(copy.deepcopy(capability_config))
    return config


def _render_reference_capability(attachment, capability, context):
    kind = capability.get("kind") or capability.get("capability_id")
    if kind not in _reference_derived_view(context.get("profile") or {}):
        return ""
    if reference_capability_errors(attachment, capability, context):
        return ""
    config = effective_reference_config(attachment, capability, context)
    labels = _reference_labels(attachment, context)
    if kind == "derived_prompt":
        item_id = str(attachment.get("source", {}).get("reference_item_id") or "")
        row = (context.get("generic_references") or {}).get(item_id) or {}
        return str(row.get("prompt") or "").strip()
    if kind in {"definitions", "retention"}:
        return "\n".join(text for _owner, text in reference_capability_lines(
            attachment, capability, context))
    if kind == "mentions":
        return str(config.get("text") or " ".join(labels)).strip()
    if kind == "summary":
        task_types = _minimax_task_types(
            context, config.get("task_types") or [], context.get("profile"))
        prefix = f"[{' + '.join(task_types)}] " if task_types else ""
        return f"{prefix}{str(config.get('summary') or '').strip()}".strip()
    if kind == "audio_relationship":
        return str(config.get("audio_relationship") or "").strip()
    return ""


# Segment kinds a rendered Reference line decomposes into. `text` is authored
# prose; every other kind is DERIVED — labels renumber, source lists change
# arity, shot citations follow a render-window-dependent counter, markers follow
# staged intent, and speaker tokens follow document order. Freezing any of them
# into stored prose violates the ordinal invariant.
#
# EXPIRY: `DERIVED_SEGMENT_KINDS` currently has no reader — its two consumers
# went with the materializer. It is held for the prose-read renderer and Convert
# to prose, which need exactly this authored/derived split. Delete it if both are
# abandoned; do not delete it merely because nothing imports it today.
SEGMENT_TEXT = "text"
SEGMENT_LABEL = "label"
SEGMENT_SOURCES = "sources"
SEGMENT_SHOTS = "shots"
SEGMENT_MARKER = "marker"
SEGMENT_SPEAKER = "speaker"
DERIVED_SEGMENT_KINDS = frozenset({
    SEGMENT_LABEL, SEGMENT_SOURCES, SEGMENT_SHOTS, SEGMENT_MARKER,
    SEGMENT_SPEAKER,
})


def _segment(kind, text, **fields) -> dict:
    """One run of a rendered line, tagged with whether it is authored.

    `text` is always the exact substring this segment contributes, so
    `"".join(segment["text"] for segment in segments)` reproduces the assembled
    line byte for byte. That identity is what lets a renderer walk the segments
    instead of re-parsing the line: the assemblers build segments and join them,
    rather than building a string and picking it apart again.
    """
    return {"kind": kind, "text": str(text or ""), **fields}


def segments_text(segments) -> str:
    """The rendered line a segment list stands for."""
    return "".join(str(segment.get("text") or "") for segment in segments or [])


def reference_capability_segments(attachment, capability, context) -> list[tuple]:
    """`reference_capability_lines`, before the segments are joined.

    Same owner keys and same order; each line carries its segment list instead
    of its rendered string.

    EXPIRY: no production caller yet — this is the seam the prose-read renderer
    and Convert to prose consume, since they need to know which runs the author
    owns and which the compiler derives. Delete it if both are abandoned.
    """
    return _reference_capability_parts(attachment, capability, context)


def convert_capability_plan(attachment, capability, context) -> dict:
    """What "Convert to prose" would write, or why it refuses.

    Convert is the author's one-way escape hatch: it turns a chip's rendered
    contribution into prose they own outright. The rule it has to satisfy is
    NOT "prose is better" — it is that no ordinal may become stored text.
    `PromptSection.channels` is derived from the channel documents
    (`channel_document_mirrors`) and persisted to `project.json`, so anything
    Convert writes into a document reaches disk; freezing `<Subject 1>` or
    `[Shot 2]` there would make the file say something staging no longer agrees
    with, which is the ordinal invariant.

    So each derived segment kind must either have a LIVE spelling or block the
    conversion:

    * `label`   -> a handle for the semantic unit. Renumbers as before.
    * `sources` -> a handle per contributing member (needs `unit_source_members`).
    * `marker`  -> frozen as text. `fully_preserved` is an enum drawn from staged
                   intent, not an ordinal, so freezing it loses tracking but
                   writes nothing positional to disk. Disclosed, never silent.
    * `shots`   -> REFUSED. The segment carries rendered numbers, not shot ids
                   (`appearances` is a list of ordinals), so `@shot(id)` cannot
                   be built from here and the only alternative is freezing
                   `[Shot 2]`, which the invariant forbids. Lifting this needs a
                   `unit_shot_ids` sidecar beside `unit_source_members`.
    * `speaker` -> REFUSED. `(S1)` is an ordinal and has no handle spelling at
                   all.

    Returns ids rather than handles: the browser owns handle resolution and
    attachment minting, and duplicating that here would be a second authority
    over the same fact.
    """
    plan = {"lines": [], "refused": "", "frozen": []}
    blockers = set()
    for owner, segments in reference_capability_segments(
            attachment, capability, context):
        parts = []
        for segment in segments:
            kind = str(segment.get("kind") or "")
            text = str(segment.get("text") or "")
            if kind == SEGMENT_TEXT:
                if text:
                    parts.append({"kind": "text", "text": text})
            elif kind == SEGMENT_LABEL:
                unit_id = str(segment.get("unit_id") or "")
                if not unit_id:
                    # A PHYSICAL definition's label has no semantic unit behind
                    # it — the owner is keyed by the rendered `<Picture 1>`
                    # itself, which is the ordinal-as-identity problem recorded
                    # in the withdrawn materializer plan. With no id there is no
                    # handle to emit and the only alternative is freezing the
                    # ordinal, so the line blocks like any other.
                    blockers.add("an entity with no stable id")
                    continue
                parts.append({"kind": "handle", "source": "unit",
                              "id": unit_id, "rendered": text})
            elif kind == SEGMENT_SOURCES:
                parts.append({"kind": "sources", "rendered": text,
                              "member_ids": [str(value) for value
                                             in segment.get("member_ids") or []]})
            elif kind == SEGMENT_MARKER:
                if text:
                    parts.append({"kind": "text", "text": text, "frozen": True})
                    plan["frozen"].append(text.strip())
            elif kind in DERIVED_SEGMENT_KINDS:
                # An EMPTY derived segment contributes nothing to the rendered
                # line, so there is no number to freeze and nothing to block.
                # Retention always carries a `shots` segment, empty when the
                # Reference is staged in no shot; treating its mere presence as
                # a blocker refused every retention line and left the marker
                # freeze rule with no case that reached it.
                if text.strip():
                    blockers.add(kind)
        if parts:
            plan["lines"].append({"owner": record_owner_key(owner), "parts": parts})
    if blockers:
        named = ", ".join(sorted(blockers))
        plan["refused"] = (
            f"This contribution cites {named}, which follow staging and the "
            "render window and have no handle spelling. Converting would "
            "freeze a number that staging can still change.")
        # A refusal emits NOTHING. Leaving `frozen` populated would report a
        # freeze that never happened, and a caller disclosing it would warn the
        # author about a change it did not make.
        plan["lines"] = []
        plan["frozen"] = []
    return plan


def record_owner_key(owner) -> str:
    """Readable owner tuple, for reporting only.

    Deliberately NOT an identity: the retention/audio owners embed a rendered
    label, which is why per-LINE conversion is not offered and Convert operates
    on a whole capability. See the withdrawn materializer plan for the full
    argument.
    """
    if isinstance(owner, (list, tuple)):
        return ":".join(str(part) for part in owner)
    return str(owner or "")


def reference_capability_lines(attachment, capability, context) -> list[tuple]:
    """Owner-tagged rendered lines. Thin join over the segment builder, so the
    string path and the materialized path can never disagree."""
    return [(owner, segments_text(segments)) for owner, segments
            in _reference_capability_parts(attachment, capability, context)]


def _reference_capability_parts(attachment, capability, context) -> list[tuple]:
    """Owner-tagged definition/retention lines for per-semantic-unit dedupe.

    Two chips may legitimately select overlapping Subjects ([A, B] and [A]).
    Deduping by the complete selection tuple emits A twice and lets two
    contradictory definitions through without a `conflicting_emission`, so the
    owner key must be the semantic unit or physical slot, not the chip.
    """
    kind = capability.get("kind") or capability.get("capability_id")
    config = effective_reference_config(attachment, capability, context)
    labels = _reference_labels(attachment, context)
    units = context.get("semantic_units_by_id") or {}
    profile = context.get("profile") or {}
    visual_prefixes = [
        declared_label_prefix(declaration)
        for declaration in physical_population_declarations(profile)
        if str(declaration.get("token_kind") or "") in {"picture", "video"}
        and declared_label_prefix(declaration)
    ]
    audio_prefixes = [
        declared_label_prefix(declaration)
        for declaration in physical_population_declarations(profile)
        if str(declaration.get("token_kind") or "") == "audio"
        and declared_label_prefix(declaration)
    ]
    if kind == "definitions":
        lines = []
        for unit_id in attachment["source"].get("semantic_unit_ids") or []:
            unit = units.get(str(unit_id)) or {}
            identity_kind = str(unit.get("kind") or "subject")
            identity_declaration = identity_kind_for(
                context.get("profile") or {}, identity_kind)
            number = (context.get("ordinal_manifest", {}).get(
                identity_ordinal_key(identity_kind)) or {}).get(str(unit_id))
            assetless = not unit.get("sources")
            identity_label = declared_label(
                identity_declaration, number,
                "assetless_label_template" if assetless
                else "referenced_label_template")
            definition, definition_source = _subject_definition(
                config, unit, context)
            source = ""
            source_labels = context.get("unit_source_labels", {}).get(str(unit_id)) or []
            source_members = context.get("unit_source_members", {}).get(str(unit_id)) or []
            # Filter both together so the member list stays aligned with the
            # labels it describes; the two are index-parallel by construction
            # and every filter has to preserve that or a source resolves to the
            # wrong Reference.
            visual_pairs = [(str(label), str(source_members[index])
                             if index < len(source_members) else "")
                            for index, label in enumerate(source_labels)
                            if any(str(label).startswith(prefix)
                                   for prefix in visual_prefixes)]
            visual_labels = [label for label, _member in visual_pairs]
            if visual_labels:
                if len(visual_labels) == 1:
                    joined_labels = visual_labels[0]
                else:
                    joined_labels = ", ".join(visual_labels[:-1]) + f", and {visual_labels[-1]}"
                source = f" from {joined_labels}"
            if identity_label and definition:
                body = _definition_before_source(definition, source)
                lines.append((("subject_definition", str(unit_id)), [
                    _segment(SEGMENT_LABEL, identity_label,
                             unit_id=str(unit_id), identity_kind=identity_kind),
                    _segment(SEGMENT_TEXT, " is "),
                    # `origin` names the tier the authored prose came from
                    # (chip, identity, or the member's Library text), so a
                    # surface rendering this line can tell the author
                    # whether editing here creates an override or edits the
                    # thing everything else inherits. Resolved here because
                    # the fallback chain lives here; every caller used to
                    # discard it.
                    # EXPIRY: no consumer yet. It is here for the Writing
                    # contribution blocks to say whether editing a line
                    # overrides or edits the shared source. Drop it if that
                    # attribution is abandoned.
                    _segment(SEGMENT_TEXT, body, authored=True,
                             origin=definition_source),
                    # Variable arity: one source reads " from <Picture 1>", two
                    # gain an Oxford join. The count follows staging, so the
                    # whole suffix stays derived even though the prose before
                    # it is authored.
                    _segment(SEGMENT_SOURCES, source, labels=list(visual_labels),
                             member_ids=[member for _label, member in visual_pairs]),
                ]))
            elif assetless and definition:
                speaker_number = (context.get("speaker_order") or {}).get(
                    str(unit_id))
                if not speaker_number:
                    lines.append((("identity_definition", str(unit_id)),
                                  [_segment(SEGMENT_TEXT, definition,
                                            authored=True)]))
            audio_definition = str(config.get("audio_definition") or "").strip()
            speaker_subject_id = str(config.get("audio_speaker_subject_id") or "")
            speaker_suffix = ""
            if speaker_subject_id:
                speaker_unit = units.get(speaker_subject_id) or {}
                speaker_kind = str(speaker_unit.get("kind") or "subject")
                speaker_declaration = identity_kind_for(
                    context.get("profile") or {}, speaker_kind)
                speaker_subject_number = (context.get("ordinal_manifest", {}).get(
                    identity_ordinal_key(speaker_kind)) or {}).get(speaker_subject_id)
                speaker_number = (context.get("speaker_order") or {}).get(
                    speaker_subject_id)
                speaker_label = declared_label(
                    speaker_declaration, speaker_subject_number,
                    "referenced_label_template")
                speaker_token = declared_label(
                    effective_speaker_policy(context.get("profile") or {}),
                    speaker_number, "token_template")
                if speaker_label and speaker_token:
                    speaker_suffix = f" for {speaker_label} {speaker_token}"
            for audio_label in (value for value in source_labels
                                if any(str(value).startswith(prefix)
                                       for prefix in audio_prefixes)):
                if audio_definition:
                    lines.append((("audio_definition", str(audio_label)), [
                        _segment(SEGMENT_LABEL, audio_label),
                        _segment(SEGMENT_TEXT, " is "),
                        _segment(SEGMENT_TEXT, audio_definition, authored=True),
                        # Carries BOTH a renumbering label and a document-order
                        # speaker token, so it is derived twice over.
                        _segment(SEGMENT_SPEAKER, speaker_suffix,
                                 subject_id=speaker_subject_id),
                    ]))
        manifest = context.get("ordinal_manifest") or {}
        for declaration in physical_population_declarations(
                context.get("profile") or {}):
            source_key = str(declaration.get("source_key") or "")
            population = str(declaration.get("ordinal_key") or "")
            for source_id in attachment["source"].get(source_key) or []:
                number = (manifest.get(population) or {}).get(str(source_id))
                definition = str((
                    config.get("audio_definition")
                    if str(declaration.get("token_kind") or "") == "audio"
                    else config.get("definition")) or "").strip()
                # Authored Library prose is the definition of last resort, so a
                # chip left blank FOLLOWS its member instead of emitting
                # nothing. Mirrors `_subject_definition`'s member fallback one
                # tier down, and keeps the chip editor's inherited preview and
                # the compiled output telling the same story.
                if not definition:
                    definition = str((_members_by_id(context).get(
                        str(source_id)) or {}).get("prompt") or "").strip()
                label = declared_label(declaration, number)
                if label and definition:
                    lines.append((("physical_definition", population, str(source_id)), [
                        _segment(SEGMENT_LABEL, label,
                                 population=population, source_id=str(source_id)),
                        _segment(SEGMENT_TEXT, " is "),
                        _segment(SEGMENT_TEXT, definition, authored=True),
                    ]))
        return lines
    if kind == "retention":
        lines = []
        visual_intent = str(config.get("visual_intent") or "")
        audio_intent = str(config.get("audio_intent") or "")
        profile = context.get("profile") or {}
        identity_prefixes = [
            (declared_label_prefix(declaration, "referenced_label_template"), declaration)
            for declaration in identity_kind_declarations(profile)
            if declared_label_prefix(declaration, "referenced_label_template")
        ]
        physical_prefixes = [
            (declared_label_prefix(declaration), declaration)
            for declaration in physical_population_declarations(profile)
            if declared_label_prefix(declaration)
        ]
        for label in labels:
            unit_id = ""
            identity_declaration = next((declaration for prefix, declaration
                                         in identity_prefixes
                                         if label.startswith(prefix)), None)
            physical_declaration = next((declaration for prefix, declaration
                                         in physical_prefixes
                                         if label.startswith(prefix)), None)
            if identity_declaration is not None:
                number = re.search(r"\d+", label)
                ordinal_key = identity_ordinal_key(
                    identity_declaration.get("key") or "subject")
                unit_id = next((value for value in
                                attachment["source"].get("semantic_unit_ids") or []
                                if str((context.get("ordinal_manifest", {}).get(
                                    ordinal_key) or {}).get(str(value)))
                                == (number.group(0) if number else "")), "")
                unit = units.get(str(unit_id)) or {}
                resolved_visual = visual_intent or str(
                    unit.get("visual_intent") or "preserve")
                resolved_audio = audio_intent or str(
                    unit.get("audio_intent") or "reference_characteristics")
                member_ids = {str(value.get("member_id") or "")
                              for value in unit.get("sources") or []
                              if isinstance(value, dict)}
                applicable = [row for row in (
                    (context.get("setup_manifest", {}).get("pictures") or [])
                    + (context.get("setup_manifest", {}).get("videos") or [])
                    + (context.get("setup_manifest", {}).get("standalone_audios") or []))
                              if str(row.get("member_id") or
                                     row.get("video_member_id") or "") in member_ids]
                staged_visual = {str(row.get("visual_intent") or "")
                                 for row in applicable if row.get("visual_intent")}
                staged_audio = {str(row.get("audio_intent") or "")
                                for row in applicable if row.get("audio_intent")}
                if not visual_intent and len(staged_visual) == 1:
                    resolved_visual = next(iter(staged_visual))
                if not audio_intent and len(staged_audio) == 1:
                    resolved_audio = next(iter(staged_audio))
            else:
                ordinal_match = re.search(r"\d+", label)
                ordinal = int(ordinal_match.group(0)) if ordinal_match else 0
                setup = context.get("setup_manifest") or {}
                population_key = str((physical_declaration or {}).get("key") or "")
                ordinal_field = (f"{str((physical_declaration or {}).get('token_kind') or '')}"
                                 "_ordinal")
                staged_row = next((row for row in setup.get(population_key) or []
                                   if int(row.get(ordinal_field) or 0) == ordinal), {})
                resolved_visual = visual_intent or str(
                    staged_row.get("visual_intent") or "preserve")
                resolved_audio = audio_intent or str(
                    staged_row.get("audio_intent") or
                    "reference_characteristics")
            is_audio = str((physical_declaration or {}).get("token_kind") or "") == "audio"
            marker = AUDIO_INTENTS.get(resolved_audio, "reference") if is_audio \
                else VISUAL_INTENTS.get(resolved_visual, "fully_preserved")
            unit_shots = context.get("reference_unit_shots") or {}
            appearances = (unit_shots.get(str(unit_id)) if identity_declaration is not None
                           else None) or (context.get("reference_group_shots") or {}).get(
                               attachment.get("emission_group_id"), [])
            appearance = ""
            if identity_declaration is not None and appearances:
                appearance = " (appears in " + ", ".join(
                    f"[Shot {number}]" for number in appearances) + ")"
            details = config.get("retention_details")
            detail = ""
            if isinstance(details, dict):
                detail = str(details.get(label) or "").strip()
            detail = detail or str(config.get("retention_detail") or "").strip()
            owner = (("retention_subject", str(unit_id))
                     if identity_declaration is not None
                     else ("retention_physical", str(label)))
            segments = [
                _segment(SEGMENT_LABEL, label, unit_id=str(unit_id)),
                # Render-window dependent: the same Reference cites different
                # shots under a different window, and an earlier shot renumbers
                # every citation after it. Never storable as text.
                _segment(SEGMENT_SHOTS, appearance, shots=list(appearances or [])),
                _segment(SEGMENT_TEXT, ": "),
                _segment(SEGMENT_MARKER, marker,
                         visual_intent=resolved_visual,
                         audio_intent=resolved_audio, is_audio=is_audio),
            ]
            if detail:
                segments.append(_segment(SEGMENT_TEXT, " - "))
                segments.append(_segment(SEGMENT_TEXT, detail, authored=True))
            lines.append((owner, segments))
        return lines
    return []


def _render_generic(attachment, capability, context, speaker_numbers=None):
    kind = attachment["kind"]
    config = (effective_reference_config(attachment, capability, context)
              if kind == "reference" else
              {**attachment.get("config", {}), **capability.get("config", {})})
    if kind == "vocal_event":
        rendered_attachment = attachment
        if not str(config.get("subject_phrase") or "").strip():
            subject_ids = [str(value) for value in
                           attachment.get("source", {}).get("subject_ids") or []
                           if str(value)]
            if len(subject_ids) == 1:
                unit = (context.get("semantic_units_by_id") or {}).get(
                    subject_ids[0]) or {}
                if not unit.get("sources") and str(unit.get("definition") or "").strip():
                    rendered_attachment = copy.deepcopy(attachment)
                    rendered_attachment.setdefault("config", {})["subject_phrase"] = str(
                        unit.get("definition") or "").strip()
        return _render_vocal_event(
            rendered_attachment, speaker_numbers or [],
            effective_speaker_policy(context.get("profile") or {}))
    if kind == "reference":
        return _render_reference_capability(attachment, capability, context)
    if kind == "custom":
        declaration = _custom_declaration(context.get("profile"))
        formatter = str(declaration.get("formatter") or "")
        if formatter:
            declared = _custom_field_declarations(declaration)
            authored = _custom_authored_fields(config)
            value = formatter
            # Only declared field names substitute, and only with a value from
            # that field's declared enum.  An API-authored config may carry
            # anything; substituting it unchecked is exactly the bounded-enum
            # bypass this guards, and `custom_capability_errors` reports it.
            for name, field in declared.items():
                allowed = {choice["value"] for choice in declared_field_choices(field)}
                authored_value = str(authored.get(name) or "")
                value = value.replace(
                    "{" + name + "}",
                    authored_value if authored_value in allowed else "")
            for name in ("text", "value"):
                if name in declared:
                    continue
                value = value.replace("{" + name + "}",
                                      str(config.get("text") or ""))
            return value.strip()
        return str(config.get("text") or "").strip()
    return ""


def _custom_declaration(profile) -> dict:
    declaration = ((profile or {}).get("capabilities") or {}).get("custom")
    return declaration if isinstance(declaration, dict) else {}


def _custom_field_declarations(declaration) -> dict:
    fields = declaration.get("fields")
    return {str(name): value for name, value in fields.items()
            if isinstance(value, dict)} if isinstance(fields, dict) else {}


def _custom_authored_fields(config) -> dict:
    """Authored substitution values: an explicit `fields` bag, else flat config."""
    fields = config.get("fields")
    source = fields if isinstance(fields, dict) else config
    return {str(name): value for name, value in source.items()}


def custom_capability_errors(attachment, capability, profile) -> list[dict]:
    """Validate authored custom fields against the profile's declaration."""
    declaration = _custom_declaration(profile)
    formatter = str(declaration.get("formatter") or "")
    if not formatter:
        return []
    declared = _custom_field_declarations(declaration)
    config = {**(attachment.get("config") or {}),
              **((capability or {}).get("config") or {})}
    authored = _custom_authored_fields(config)
    attachment_id = str(attachment.get("attachment_id") or "")
    errors = []
    substitutions = set(re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", formatter))
    # An explicit `fields` bag states substitution intent, so every name in it
    # must be declared. A flat legacy config is not checked for extra keys: it
    # doubles as the chip's own configuration and carries unrelated state.
    candidates = set(authored) if isinstance(config.get("fields"), dict) else (
        set(authored) & set(declared))
    checked = set()
    for name in sorted(substitutions | candidates):
        if name in checked:
            continue
        checked.add(name)
        field = declared.get(name)
        if field is None:
            if name in {"text", "value"}:
                continue
            errors.append({
                "code": "unknown_custom_capability_field",
                "attachment_id": attachment_id,
                "message": f"Custom Context field {name!r} is not declared by this prompt format.",
            })
            continue
        allowed = [choice["value"] for choice in declared_field_choices(field)]
        raw_value = authored.get(name)
        if raw_value is None or not str(raw_value).strip():
            if name in substitutions:
                errors.append({
                    "code": "missing_custom_capability_field",
                    "attachment_id": attachment_id,
                    "message": f"Custom Context field {name!r} is required by this prompt format.",
                })
            continue
        if str(raw_value) not in allowed:
            errors.append({
                "code": "invalid_custom_capability_field",
                "attachment_id": attachment_id,
                "message": (f"Custom Context field {name!r} value {str(raw_value)!r} "
                            "is not one of its declared values."),
            })
    return errors


def reference_capability_errors(attachment, capability, context) -> list[dict]:
    """Validate enabled Reference state against its resolved format vocabulary."""
    profile = context.get("profile") or {}
    kind = str(capability.get("kind") or capability.get("capability_id") or "")
    declaration = _reference_derived_view(profile).get(kind)
    attachment_id = str(attachment.get("attachment_id") or "")
    if not isinstance(declaration, dict):
        return [{
            "code": "undeclared_reference_capability",
            "attachment_id": attachment_id,
            "capability_id": str(capability.get("capability_id") or ""),
            "message": (f"Reference capability {kind!r} is not declared by "
                        f"{profile.get('name') or 'this prompt format'}."),
        }]
    config = effective_reference_config(attachment, capability, context)
    errors = []
    for field_name, field in (declaration.get("fields") or {}).items():
        if field_name not in config:
            continue
        allowed = {choice["value"] for choice in declared_field_choices(field)}
        raw = config.get(field_name)
        values = raw if field.get("type") == "enum_multi" else [raw]
        if not isinstance(values, list):
            values = [values]
        unknown = [str(value) for value in values if str(value) not in allowed]
        if unknown:
            errors.append({
                "code": "unknown_declared_field_value",
                "attachment_id": attachment_id,
                "capability_id": str(capability.get("capability_id") or ""),
                "field": str(field_name),
                "message": (f"Reference field {field_name!r} contains values "
                            f"outside this prompt format: {', '.join(unknown)}."),
            })
    return errors


def _document_render(document, attachment_by_id, render_anchor):
    parts = []
    for node in normalize_prompt_document(document)["nodes"]:
        if node["type"] == "text":
            parts.append(node["text"])
            continue
        attachment = attachment_by_id.get(node["attachment_id"])
        if attachment and attachment["enabled"]:
            parts.append(render_anchor(
                attachment, node.get("capability_id"), node.get("node_id")))
    return "".join(parts).strip()


def _default_capability(attachment, profile):
    declaration = (profile.get("capabilities") or {}).get(attachment["kind"], {})
    capability_kind = attachment["kind"]
    if attachment["kind"] == "reference":
        derived = _reference_derived_view(profile)
        if derived:
            capability_kind, capability_declaration = min(
                derived.items(), key=lambda item: (
                    int(item[1].get("order", MAX_CAPABILITIES))
                    if isinstance(item[1], dict) else MAX_CAPABILITIES,
                    item[0]))
            declaration = capability_declaration
    return normalize_capability({
        "capability_id": capability_kind,
        "kind": capability_kind,
        "channel_key": declaration.get("channel_key", ""),
        "placement": declaration.get("placement", "section_prefix"),
    })


def _resolved_capability(attachment, capability, profile) -> dict:
    value = copy.deepcopy(capability)
    # `enabled` is tri-state in storage but every renderer below reads it as a
    # plain bool, several by bracket access. The compile resolves stored records
    # against their shared default up front; anything still absent here was
    # never stored at all — a synthesized default capability — and is enabled.
    value.setdefault("enabled", True)
    if value.get("placement"):
        return value
    attachment_declaration = ((profile.get("capabilities") or {}).get(
        attachment["kind"]) or {})
    kind = str(value.get("kind") or value.get("capability_id") or "")
    derived = (attachment_declaration.get("derived")
               if isinstance(attachment_declaration, dict) else {})
    kind_declaration = (derived.get(kind) if isinstance(derived, dict) else {})
    value["placement"] = str(
        (kind_declaration or {}).get("placement")
        or attachment_declaration.get("placement")
        or "section_prefix")
    return value


def _capabilities(attachment, profile):
    values = attachment["capabilities"] or [_default_capability(attachment, profile)]
    resolved = [_resolved_capability(attachment, value, profile) for value in values]
    return [value for value in resolved
            if value.get("placement") in PLACEMENT_PHASES]


SCOPE_LINK_CHANNEL_CAPABILITY_PREFIX = "prompt_link_scope:"


def scope_link_channel_capability_id(channel) -> str:
    """The capability id a section-scope Prompt Link uses for one channel."""
    return f"{SCOPE_LINK_CHANNEL_CAPABILITY_PREFIX}{channel}"


def _scope_link_capability(attachment, profile, channel):
    """One capability per selected channel, so suppression can be per channel.

    A scope link used to carry exactly ONE capability reused for every channel,
    which is why muting it was all-or-nothing while a Reference — many
    capabilities, each with its own id — could suppress one part and keep the
    rest. Splitting it per channel lets the existing per-capability `enabled`
    flag and its projection row do the work, rather than inventing a second,
    parallel `disabled_channels` list over the same state.

    Records stay sparse: a channel with no stored record inherits, and the
    single legacy `prompt_link_scope` record is honoured as the inherited value
    so a chip already suppressed before this split stays suppressed instead of
    silently coming back on.
    """
    capability_id = scope_link_channel_capability_id(channel)
    stored = attachment.get("capabilities") or []
    legacy = None
    for value in stored:
        current = str(value.get("capability_id") or value.get("kind") or "")
        if current == capability_id:
            return _resolved_capability(attachment, value, profile)
        if current == "prompt_link_scope":
            legacy = value
    base = copy.deepcopy(legacy) if legacy else _default_capability(
        attachment, profile)
    base["capability_id"] = capability_id
    base["kind"] = "prompt_link_scope"
    return _resolved_capability(attachment, base, profile)


def _enabled_capabilities(attachment, profile):
    """Capabilities that actually render.

    Rendering already skips disabled capabilities, so validation and config
    merging must skip them too; otherwise a disabled capability can block a
    job or contribute configuration to text it never appears in.
    """
    return [value for value in _capabilities(attachment, profile)
            if value.get("enabled", True)]


def _route_for(attachment, capability, profile, fallback_channel):
    if capability.get("channel_key"):
        return capability["channel_key"]
    declaration = (profile.get("capabilities") or {}).get(attachment["kind"], {})
    kind = capability.get("kind") or capability.get("capability_id")
    derived = declaration.get("derived") if isinstance(declaration, dict) else {}
    capability_declaration = (derived.get(kind)
                              if isinstance(derived, dict) else {})
    return str((capability_declaration or {}).get("channel_key")
               or declaration.get("channel_key") or fallback_channel)


_PROFILE_ERROR_MESSAGES = {
    "unknown_profile": "The selected prompt format is not available in this project.",
    "profile_template_incompatible": ("The selected prompt format is not compatible "
                                      "with the active channel template."),
}


def profile_error_result(exc, *, window_start=0, window_end=1, fps=24.0) -> dict:
    """A compiled-shaped payload carrying one blocking profile diagnostic.

    An unusable profile cannot produce trustworthy text, but it is an authoring
    mistake rather than a server fault, so every surface receives the normal
    result contract with a blocking error instead of a 500.  Only
    `ProfileResolutionError` reaches here; a plain ValueError from anywhere else
    in compilation is a real bug and must keep its traceback.
    """
    code = getattr(exc, "code", "") or "invalid_profile"
    result = {
        "format": FORMAT_VERSION, "prompt": "", "channels": {}, "segments": [],
        "relay": prompt_payload.build_relay_payload("", []),
        "window": {"start_frame": int(window_start), "end_frame": int(window_end),
                   "fps": float(fps)},
        "emissions": [], "attachment_previews": {},
        "attachment_channel_previews": {},
        "attachment_channel_routes": {},
        "attachment_capability_projections": [],
        "managed_speaker_subject_ids": [],
        "setup_manifest": {}, "ordinal_manifest": {},
        "profile": {}, "profile_hash": "",
        "warnings": [],
        "errors": [{"code": code, "detail": str(exc),
                    "message": _PROFILE_ERROR_MESSAGES.get(
                        code, f"Prompt Context profile is invalid ({exc}).")}],
    }
    result["content_hash"] = content_hash({
        "prompt": "", "channels": {}, "segments": [],
        "window": result["window"], "profile_hash": "", "setup_manifest": {},
    })
    return result


def _locate_capability(convert_plan_for, global_attachments, sections):
    """The staged (attachment, capability) a Convert request names, or None.

    Searches the compiled state itself rather than trusting the request, so a
    Convert cannot name a capability the author cannot see staged.
    """
    if not isinstance(convert_plan_for, dict):
        return None
    attachment_id = str(convert_plan_for.get("attachment_id") or "")
    capability_id = str(convert_plan_for.get("capability_id") or "")
    if not attachment_id or not capability_id:
        return None
    pools = [global_attachments or []]
    for section in sections or []:
        pools.append(getattr(section, "attachments", None)
                     or (section.get("attachments") if isinstance(section, dict) else None)
                     or [])
    for pool in pools:
        for raw in pool:
            attachment = normalize_attachment(raw)
            if attachment.get("attachment_id") != attachment_id:
                continue
            for capability in attachment.get("capabilities") or []:
                if str(capability.get("capability_id") or "") == capability_id:
                    return attachment, capability
    return None


def compile_prompt_context(*, global_documents=None, global_channels=None,
                           global_attachments=None, sections=None,
                           window_start=0, window_end=1, fps=24.0,
                           template=None, profile=None, custom_profiles=None,
                           context=None, labels_on=True,
                           delimiter=prompt_payload.DEFAULT_SECTION_DELIMITER,
                           boundary_threshold_pct=0.0,
                           convert_plan_for=None) -> dict:
    """Compile candidate state to a frozen, provider-ready prompt.

    The function never mutates inputs.  It returns diagnostics instead of
    throwing for semantic authoring mistakes; invalid/unknown profiles remain
    a hard error because their formatter cannot be trusted.
    """
    context = copy.deepcopy(context) if isinstance(context, dict) else {}
    resolved_template = prompt_channel_templates.get_channel_template(template)
    resolved_profile = resolve_profile(profile, template=resolved_template,
                                       custom_profiles=custom_profiles)
    context["profile"] = copy.deepcopy(resolved_profile)
    keys = prompt_channel_templates.template_channel_keys(resolved_template)
    global_docs = normalize_channel_documents(global_documents, global_channels, keys)
    global_mirror = channel_document_mirrors(global_docs)
    errors, warnings, emissions = [], [], []
    setup_manifest = context.get("setup_manifest") or {}
    role_catalogs = resolved_profile.get("role_catalogs") or {}
    for declaration in physical_population_declarations(resolved_profile):
        manifest_key = str(declaration.get("key") or "")
        population = manifest_key
        allowed_roles = {str(value.get("value") or "") for value in
                          role_catalogs.get(population, [])
                         if isinstance(value, dict)}
        identity_source_members = {
            str((source or {}).get("member_id") or "")
            for unit in context.get("semantic_units") or []
            if isinstance(unit, dict)
            for source in unit.get("sources") or []
        }
        for row in setup_manifest.get(manifest_key) or []:
            role = str(row.get("role") or "").strip()
            if role and (not allowed_roles or role not in allowed_roles):
                errors.append({
                    "code": "unsupported_reference_role",
                    "member_id": str(row.get("member_id") or ""),
                    "message": (f"Reference role {role!r} is not supported by "
                                f"{resolved_profile.get('name') or 'this prompt format'}; rebind it."),
                })
            # A staged member with neither a prompt identity nor authored prose
            # reaches the model as pixels the text never names. Keyed on the
            # MEMBER, deliberately not folded into `missing_h3_reference_field`:
            # that one is the format-labelled variant of `empty_channel` and
            # names an empty prompt channel, so retargeting it would destroy a
            # different diagnostic and still not name the missing identity.
            member_id = str(row.get("member_id") or row.get("video_member_id") or "")
            if (member_id and member_id not in identity_source_members
                    and not str(row.get("member_prompt") or "").strip()):
                warnings.append({
                    "code": "unnamed_physical_reference",
                    "member_id": member_id,
                    "message": (
                        f"{row.get('display_name') or 'This staged Reference'} has no "
                        "prompt identity and no Library prompt text, so nothing in "
                        "the prompt refers to it. Use + Identity on its row, or "
                        "give it prompt text under Defaults."),
                })
    raw_sections = []
    global_attachment_values = normalize_attachments(global_attachments)
    for attachment in global_attachment_values:
        if attachment.get("kind") in {"shot", "timestamp", "prompt_link",
                                       "prompt_link_scope"}:
            errors.append({
                "code": "invalid_global_attachment",
                "attachment_id": attachment.get("attachment_id", ""),
                "message": (f"{attachment.get('kind')} is section-scoped; "
                            "move this chip to a prompt section."),
            })
        elif attachment.get("kind") == "vocal_event":
            errors.append({
                "code": "global_vocal_event",
                "attachment_id": attachment.get("attachment_id", ""),
                "message": ("Vocal Events require a section/inline position so "
                            "speaker order is deterministic; move this chip to a section."),
            })
    scene_attachments = list(global_attachment_values)

    for raw_index, raw in enumerate(sections or []):
        if isinstance(raw, dict):
            value = copy.deepcopy(raw)
        else:
            value = {
                "prompt_id": getattr(raw, "prompt_id", ""),
                "start_frame": getattr(raw, "start_frame", 0),
                "end_frame": getattr(raw, "end_frame", 0),
                "muted": getattr(raw, "muted", False),
                "channels": getattr(raw, "channels", {}),
                "channel_docs": getattr(raw, "channel_docs", {}),
                "attachments": getattr(raw, "attachments", []),
                "global_channel_exceptions": getattr(raw, "global_channel_exceptions", []),
            }
        if not str(value.get("prompt_id") or ""):
            value["prompt_id"] = f"__compile_section_{raw_index}"
        value["attachments"] = normalize_attachments(value.get("attachments"))
        value["channel_docs"] = normalize_channel_documents(
            value.get("channel_docs"), value.get("channels"), keys)
        scene_attachments.extend(value["attachments"])
        raw_sections.append(value)

    errors.extend(attachment_limit_errors(scene_attachments))

    # Determine the effective segment origins before validating or rendering
    # section-owned Context.  Out-of-window chips are dormant for this job and
    # must not block it or claim a Prompt Link fallback.  A private sentinel
    # keeps attachment-only sections in the same first-wins/hold/threshold
    # resolver without leaking into authored output.
    preliminary = []
    for section in raw_sections:
        mirrors = channel_document_mirrors(section["channel_docs"])
        has_attachment = any(value["enabled"] for value in section["attachments"])
        if has_attachment and not any(str(mirrors.get(key) or "").strip()
                                      for key in keys) and keys:
            mirrors[keys[0]] = "\ue000"
        preliminary.append({
            "prompt_id": str(section.get("prompt_id") or ""),
            "start_frame": section.get("start_frame", 0),
            "end_frame": section.get("end_frame", 0),
            "muted": bool(section.get("muted", False)),
            "channels": mirrors,
            "_opens_shot": False, "_shot_timestamp": False,
            "global_channel_exceptions": section.get(
                "global_channel_exceptions", []),
        })
    preliminary_segments = prompt_payload.resolve_segments(
        preliminary, window_start, window_end,
        prompt_channel_templates.template_labels_on(resolved_template, labels_on),
        boundary_threshold_pct, resolved_template)
    selected_prompt_ids = {str(value.get("prompt_id") or "")
                           for value in preliminary_segments}
    selected_sections = [value for value in raw_sections
                         if str(value.get("prompt_id") or "")
                         in selected_prompt_ids]
    selected_sections.sort(key=lambda row: (
        int(row.get("start_frame", 0)), str(row.get("prompt_id") or "")))
    all_attachments = list(global_attachment_values)
    for value in selected_sections:
        all_attachments.extend(value["attachments"])
    supported_providers = supported_provider_versions(custom_profiles)
    for attachment in all_attachments:
        if not attachment.get("enabled", True):
            continue
        kind = str(attachment.get("kind") or "")
        if kind not in SUPPORTED_ATTACHMENT_KINDS:
            errors.append({
                "code": "unsupported_attachment_kind",
                "attachment_id": attachment.get("attachment_id", ""),
                "kind": kind,
                "message": f"Unsupported Context attachment kind: {kind}",
            })
        elif (kind == "timestamp"
              and not bool((attachment.get("config") or {}).get("standalone"))):
            errors.append({
                "code": "invalid_timestamp_attachment",
                "attachment_id": attachment.get("attachment_id", ""),
                "message": "A standalone Time attachment must declare config.standalone.",
            })

    def warn_authored_prompt_tokens(origin, documents):
        for channel_key, document in (documents or {}).items():
            text = prompt_document_text(document)
            if not prompt_tokens.find(text):
                continue
            warnings.append({
                "code": "authored_prompt_token_literal",
                "origin": str(origin or ""),
                "channel_key": str(channel_key or ""),
                "message": ("Prompt tokens resolve inside Context-chip fields, not "
                            "in authored prose. This text will be sent literally."),
            })

    warn_authored_prompt_tokens("global", global_docs)
    for section in selected_sections:
        warn_authored_prompt_tokens(
            section.get("prompt_id", ""), section.get("channel_docs") or {})

    context["semantic_units_by_id"] = {
        unit["semantic_unit_id"]: unit
        for unit in (normalize_semantic_unit(value)
                     for value in context.get("semantic_units") or [])
    }

    # Resolve the tri-state capability `enabled` ONCE, into the in-memory
    # compile only. Storage stays sparse — absent means inherit the shared
    # Reference/identity default — while every downstream reader keeps its plain
    # `capability["enabled"]` boolean. Resolving per read site instead would
    # thread this context through a dozen render helpers that have no business
    # knowing about Reference defaults. Must run after `semantic_units_by_id`
    # and before the first capability read.
    for attachment in [*global_attachment_values, *scene_attachments]:
        for capability in attachment.get("capabilities") or []:
            if "enabled" not in capability:
                capability["enabled"] = _inherited_capability_enabled(
                    attachment, capability, context)
    profile_validator_ids = {
        str(value) for value in resolved_profile.get("validators") or []
        if isinstance(value, str)
    }
    is_h3_reference_profile = "minimax_reference_setup" in profile_validator_ids
    is_h3_base_profile = "minimax_base_setup" in profile_validator_ids
    is_h3_profile = is_h3_reference_profile or is_h3_base_profile

    # Speaker ordering is selected-window chronological, then document order.
    vocal_events = []
    raw_index_by_id = {str(value.get("prompt_id") or ""): index
                       for index, value in enumerate(raw_sections)}
    for section in selected_sections:
        section_index = raw_index_by_id.get(
            str(section.get("prompt_id") or ""), 0)
        by_id = _attachment_map(section.get("attachments"))
        node_order = {}
        cursor = 0
        for key in keys:
            for node in section["channel_docs"].get(key, {}).get("nodes", []):
                if node.get("type") == "attachment":
                    node_order.setdefault(node.get("attachment_id"), cursor)
                    cursor += 1
        for attachment in by_id.values():
            if attachment["kind"] == "vocal_event" and attachment["enabled"]:
                vocal_events.append({"attachment": attachment,
                                     "section_index": section_index,
                                     "node_order": node_order.get(attachment["attachment_id"], 10**9)})
    vocal_events.sort(key=lambda event: (
        int(raw_sections[event["section_index"]].get("start_frame", 0)),
        event["node_order"], event["attachment"]["attachment_id"]))
    speaker_order = _speaker_bindings(vocal_events)
    context["speaker_order"] = speaker_order
    speakers_by_attachment = {
        event["attachment"]["attachment_id"]: event.get("speaker_numbers", [])
        for event in vocal_events
    }

    # Managed and literal speaker ids cannot safely share one numbering domain.
    authored_text = " ".join(global_mirror.values()) + " " + " ".join(
        value for section in selected_sections
        for value in channel_document_mirrors(section["channel_docs"]).values())
    if vocal_events and _MANUAL_SPEAKER_RE.search(authored_text):
        errors.append({"code": "managed_manual_speaker_conflict", "message":
                       "Managed Vocal Events cannot be mixed with literal (Sx) speaker ids."})
    for event in vocal_events:
        attachment = event["attachment"]
        source = attachment.get("source") or {}
        bound = [str(value) for value in source.get("subject_ids") or []
                 if str(value)] or ([str(source.get("voice_id"))]
                                    if str(source.get("voice_id") or "") else [])
        if not bound:
            # `_speaker_bindings` would otherwise invent `event:<attachment_id>`,
            # producing an (Sx) number that names nothing in the scene.
            errors.append({
                "code": "missing_vocal_binding",
                "attachment_id": attachment["attachment_id"],
                "message": ("A Vocal Event must name at least one Subject or a "
                            "stable voice; this one is unbound."),
            })
        for unit_id in bound:
            unit = context["semantic_units_by_id"].get(unit_id)
            if unit is None:
                errors.append({
                    "code": "broken_vocal_identity",
                    "attachment_id": attachment["attachment_id"],
                    "message": f"Vocal Event identity {unit_id!r} no longer exists.",
                })
                continue
            declaration = identity_kind_for(
                resolved_profile, unit.get("kind") or "subject")
            if declaration is None:
                errors.append({
                    "code": "unsupported_identity_kind",
                    "attachment_id": attachment["attachment_id"],
                    "message": f"Vocal Event identity kind {unit.get('kind')!r} is not supported by this format.",
                })
            elif declaration.get("speaks") is not True:
                errors.append({
                    "code": "identity_cannot_speak",
                    "attachment_id": attachment["attachment_id"],
                    "message": f"Prompt identity {unit.get('name') or unit_id!r} cannot own a Vocal Event in this format.",
                })
        language = str(attachment.get("config", {}).get("language") or "English")
        if language not in DIALOGUE_LANGUAGES:
            errors.append({
                "code": "invalid_vocal_event_language",
                "attachment_id": attachment["attachment_id"],
                "message": f"Managed Vocal Event language {language!r} is not in the bounded language list.",
            })
        if (is_h3_profile
                and str(attachment.get("config", {}).get("event_type") or "")
                == "voiceover"
                and not str(attachment.get("config", {}).get(
                    "subject_phrase") or "").strip()):
            errors.append({
                "code": "missing_voiceover_subject_phrase",
                "attachment_id": attachment["attachment_id"],
                "message": ("MiniMax H3 voiceover needs an authored on-screen "
                            "subject phrase; the fixed lips-closed clause is emitted automatically."),
            })

    for attachment in all_attachments:
        if not attachment["enabled"]:
            continue
        provider_id = str(attachment.get("provider_id") or "")
        provider_version = str(attachment.get("provider_version") or "")
        supported = supported_providers.get(provider_id)
        if supported is None or provider_version not in supported:
            errors.append({
                "code": "unsupported_attachment_provider",
                "attachment_id": attachment["attachment_id"],
                "message": (f"Context chip provider {provider_id or '<blank>'}@"
                            f"{provider_version or '<blank>'} is not supported by "
                            "this build."),
            })
        raw_capabilities = (attachment.get("capabilities")
                            or [_default_capability(attachment, resolved_profile)])
        for raw_capability in raw_capabilities:
            capability = _resolved_capability(
                attachment, raw_capability, resolved_profile)
            if not capability.get("enabled", True):
                continue
            placement = str(capability.get("placement") or "")
            if placement not in PLACEMENT_PHASES:
                errors.append({
                    "code": "invalid_attachment_placement",
                    "attachment_id": attachment["attachment_id"],
                    "capability_id": str(capability.get("capability_id") or ""),
                    "message": (f"Context capability placement {placement!r} is "
                                "not declared by this build; choose a supported placement."),
                })
                continue
            if attachment["kind"] == "reference":
                errors.extend(reference_capability_errors(
                    attachment, capability, context))
        if attachment["kind"] == "custom":
            for capability in _enabled_capabilities(attachment, resolved_profile):
                errors.extend(custom_capability_errors(
                    attachment, capability, resolved_profile))

    context["references_by_id"] = {
        str(value.get("reference_id") or ""): value
        for value in context.get("references") or [] if isinstance(value, dict)
        and str(value.get("reference_id") or "")
    }
    ordinal_manifest = context.get("ordinal_manifest")
    if not isinstance(ordinal_manifest, dict):
        ordinal_manifest = {}
        context["ordinal_manifest"] = ordinal_manifest
    for declaration in identity_kind_declarations(resolved_profile):
        if not str(declaration.get("assetless_label_template") or ""):
            continue
        kind = str(declaration.get("key") or "subject")
        ordinal_key = identity_ordinal_key(kind)
        target = ordinal_manifest.setdefault(ordinal_key, {})
        used = {int(value) for value in target.values()
                if isinstance(value, int) and not isinstance(value, bool)
                and value > 0}
        next_number = 1
        assetless_units = sorted(
            (unit for unit in context["semantic_units_by_id"].values()
             if str(unit.get("kind") or "subject") == kind
             and not unit.get("sources")),
            key=lambda unit: (int(unit.get("order") or 0),
                              str(unit.get("semantic_unit_id") or "")))
        for unit in assetless_units:
            unit_id = str(unit.get("semantic_unit_id") or "")
            if not unit_id or unit_id in target:
                continue
            while next_number in used:
                next_number += 1
            target[unit_id] = next_number
            used.add(next_number)
    ordinal_manifest = context.get("ordinal_manifest") or {}
    if is_h3_reference_profile:
        seen_diagnostics = set()

        def reference_diagnostic(target, code, message, attachment_id=""):
            key = (code, message, attachment_id)
            if key in seen_diagnostics:
                return
            seen_diagnostics.add(key)
            target.append({"code": code, "message": message,
                           **({"attachment_id": attachment_id}
                              if attachment_id else {})})

        def reference_error(code, message, attachment_id=""):
            reference_diagnostic(errors, code, message, attachment_id)

        def reference_warning(code, message, attachment_id=""):
            reference_diagnostic(warnings, code, message, attachment_id)

        reference_catalog_available = isinstance(context.get("references"), list)
        known_member_ids = {
            str(member.get("member_id") or "")
            for reference in context.get("references") or []
            if isinstance(reference, dict)
            for member in reference.get("members") or []
            if isinstance(member, dict)
        }
        member_populations = defaultdict(set)
        for declaration in physical_population_declarations(resolved_profile):
            population = str(declaration.get("key") or "")
            for row in context.get("setup_manifest", {}).get(population) or []:
                member_id = str(row.get("member_id") or
                                row.get("video_member_id") or "")
                if member_id:
                    member_populations[member_id].add(population)
        contribution_catalog = effective_contribution_catalog(resolved_profile)

        for attachment in all_attachments:
            if not attachment["enabled"] or attachment["kind"] != "reference":
                continue
            source = attachment.get("source") or {}
            unit_ids = [str(value) for value in
                        source.get("semantic_unit_ids") or [] if str(value)]
            physical_ids = []
            for declaration in physical_population_declarations(resolved_profile):
                source_key = str(declaration.get("source_key") or "")
                physical_ids.extend(str(value) for value in source.get(source_key) or []
                                    if str(value))
            if not unit_ids and not physical_ids:
                reference_error(
                    "missing_reference_source",
                    "A MiniMax H3 Reference Context chip has no semantic or physical source.",
                    attachment["attachment_id"])
            config = effective_reference_config(attachment, {}, context)
            # Capability editors may own authored fields; validate the same
            # merged configuration the formatter resolves.
            for capability in _enabled_capabilities(attachment, resolved_profile):
                config.update(effective_reference_config(
                    attachment, capability, context))
            for unit_id in unit_ids:
                unit = context["semantic_units_by_id"].get(unit_id)
                if unit is None:
                    reference_error(
                        "broken_reference_source",
                        f"Reference Subject {unit_id!r} no longer exists; rebind the visible chip.",
                        attachment["attachment_id"])
                    continue
                identity_kind = str(unit.get("kind") or "subject")
                identity_declaration = identity_kind_for(
                    resolved_profile, identity_kind)
                if identity_declaration is None:
                    reference_error(
                        "unsupported_identity_kind",
                        f"Prompt identity {unit.get('name') or unit_id!r} uses unsupported kind {identity_kind!r}.",
                        attachment["attachment_id"])
                    continue
                sources = [value for value in unit.get("sources") or []
                           if isinstance(value, dict)]
                broken_sources = [value for value in sources
                                  if reference_catalog_available
                                  and str(value.get("member_id") or "")
                                  not in known_member_ids]
                if broken_sources:
                    reference_error(
                        "broken_reference_source",
                        f"Prompt identity {unit.get('name') or unit_id!r} names deleted physical Reference members.",
                        attachment["attachment_id"])
                if sources and unit_id not in (ordinal_manifest.get(
                        identity_ordinal_key(identity_kind)) or {}):
                    reference_error(
                        "reference_source_not_applicable",
                        f"Prompt identity {unit.get('name') or unit_id!r} has no winning setup member in this window.",
                        attachment["attachment_id"])
                elif not sources:
                    reference_warning(
                        "assetless_prompt_identity",
                        f"Prompt identity {unit.get('name') or unit_id!r} has no physical source and will emit descriptive prose only.",
                        attachment["attachment_id"])
                for source_row in sources:
                    contribution = str(source_row.get("contribution") or "")
                    if not contribution:
                        continue
                    member_id = str(source_row.get("member_id") or "")
                    allowed = {
                        str(value.get("value") or "")
                        for population in ["*", *sorted(member_populations.get(
                                member_id, set()))]
                        for value in contribution_catalog.get(population) or []
                        if isinstance(value, dict)
                    }
                    if contribution not in allowed:
                        reference_error(
                            "unknown_contribution",
                            f"Prompt identity source contribution {contribution!r} is not declared by this format.",
                            attachment["attachment_id"])
                if not _subject_definition(config, unit, context)[0]:
                    reference_warning(
                        "missing_h3_subject_definition",
                        f"Reference Subject {unit.get('name') or unit_id!r} needs an authored definition.",
                        attachment["attachment_id"])
                audio_prefixes = [
                    declared_label_prefix(declaration)
                    for declaration in physical_population_declarations(resolved_profile)
                    if str(declaration.get("token_kind") or "") == "audio"
                    and declared_label_prefix(declaration)
                ]
                if (any(any(str(value).startswith(prefix)
                            for prefix in audio_prefixes) for value in
                         context.get("unit_source_labels", {}).get(unit_id) or [])
                        and not str(config.get("audio_definition") or "").strip()):
                    reference_warning(
                        "missing_h3_audio_definition",
                        f"Reference Subject {unit.get('name') or unit_id!r} includes audio and needs an authored Audio definition.",
                        attachment["attachment_id"])
                speaker_subject_id = str(config.get("audio_speaker_subject_id") or "")
                if speaker_subject_id and speaker_subject_id not in speaker_order:
                    reference_error(
                        "unresolved_audio_speaker_binding",
                        "An Audio definition can reuse only a Subject that has an actual managed Vocal Event in this window.",
                        attachment["attachment_id"])
                member_ids = {str(value.get("member_id") or "")
                              for value in unit.get("sources") or []
                              if isinstance(value, dict)}
                applicable_rows = []
                for declaration in physical_population_declarations(resolved_profile):
                    applicable_rows.extend(
                        context.get("setup_manifest", {}).get(
                            str(declaration.get("key") or "")) or [])
                applicable = [row for row in applicable_rows
                              if str(row.get("member_id") or
                                     row.get("video_member_id") or "") in member_ids]
                for field in ("visual_intent", "audio_intent"):
                    staged = {str(row.get(field) or "") for row in applicable
                              if str(row.get(field) or "")}
                    if not str(config.get(field) or "").strip() and len(staged) > 1:
                        reference_error(
                            "conflicting_reference_intent",
                            f"Reference Subject {unit.get('name') or unit_id!r} has conflicting staged {field.replace('_', ' ')} values.",
                            attachment["attachment_id"])
            for declaration in physical_population_declarations(resolved_profile):
                source_key = str(declaration.get("source_key") or "")
                ordinal_key = str(declaration.get("ordinal_key") or "")
                known = ordinal_manifest.get(ordinal_key) or {}
                for source_id in source.get(source_key) or []:
                    duplicate_slots = [row for row in (
                        context.get("setup_manifest", {}).get(
                            "duplicate_member_slots", {}).get(str(source_id)) or [])
                        if isinstance(row, dict)
                        and str(row.get("population") or "") == str(
                            declaration.get("key") or "")]
                    if len(duplicate_slots) > 1:
                        reference_error(
                            "ambiguous_physical_handle",
                            f"Physical Reference {source_id!r} occupies multiple {declaration.get('label') or 'physical'} slots: "
                            + ", ".join(str(row.get("label") or row.get("slot_id") or "slot")
                                        for row in duplicate_slots) + ".",
                            attachment["attachment_id"])
                    if str(source_id) not in known:
                        reference_error(
                            "reference_source_not_applicable",
                            f"Reference source {source_id!r} is not a winning physical setup slot in this window.",
                            attachment["attachment_id"])
                        continue
                    authored = str((
                        config.get("audio_definition")
                        if str(declaration.get("token_kind") or "") == "audio" else
                        config.get("definition")) or "").strip()
                    # Decision: the member-prose fallback SUPPRESSES this
                    # warning, because the warning means "this slot emits
                    # nothing" and with prose it now emits. Leaving it armed
                    # would make the diagnostic contradict the compiled output
                    # it is describing.
                    if not authored:
                        authored = str((_members_by_id(context).get(
                            str(source_id)) or {}).get("prompt") or "").strip()
                    if not authored:
                        reference_warning(
                            "missing_h3_physical_definition",
                            f"Reference source {source_id!r} needs an authored definition.",
                            attachment["attachment_id"])
    for attachment in all_attachments:
        if not attachment["enabled"] or attachment["kind"] != "reference":
            continue
        item_id = str(attachment.get("source", {}).get("reference_item_id") or "")
        generic_reference = (context.get("generic_references") or {}).get(item_id)
        if item_id and generic_reference is None:
            errors.append({
                "code": "reference_source_not_applicable",
                "attachment_id": attachment["attachment_id"],
                "message": (f"Reference item {item_id!r} is deleted, muted, superseded, "
                            "hidden, or outside this generation window; rebind the visible chip."),
            })
        elif item_id:
            profile_key = f"{resolved_profile.get('profile_id')}@{resolved_profile.get('version')}"
            compatible = generic_reference.get("compatible_profiles") or ["generic@1"]
            if profile_key not in compatible:
                errors.append({"code": "reference_profile_incompatible",
                               "attachment_id": attachment["attachment_id"],
                               "message": f"This Reference recipe does not expose Context to {profile_key}."})
            exposed = set(generic_reference.get("exposed_capabilities") or [])
            requested = {str(value.get("kind") or "") for value in
                         _enabled_capabilities(attachment, resolved_profile)}
            if not requested.issubset(exposed):
                errors.append({"code": "reference_capability_incompatible",
                               "attachment_id": attachment["attachment_id"],
                               "message": "The Reference chip requests capabilities its recipe does not expose."})
    # Retention appearance lists are semantic aggregates over the selected
    # window. Split-derived Reference clones share an emission group, so a
    # combined render produces one definition/retention line while either half
    # remains self-contained when rendered alone.
    reference_group_shots = defaultdict(list)
    reference_unit_shots = defaultdict(list)
    # `@shot(id)` resolves against the SAME counter that numbers the markers and
    # feeds `(appears in [Shot N])`, keyed by the Shot attachment that opens the
    # shot. A section may carry more than one Shot attachment; each of them cites
    # the single shot that section opens.
    shot_ordinals = {}
    shot_number = 0
    for section in selected_sections:
        section_attachments = normalize_attachments(section.get("attachments"))
        opening_shots = [value for value in section_attachments
                         if value["enabled"] and value["kind"] == "shot"]
        if opening_shots:
            shot_number += 1
            for value in opening_shots:
                shot_ordinals[str(value["attachment_id"])] = shot_number
        if shot_number:
            for value in section_attachments:
                if value["enabled"] and value["kind"] == "reference":
                    group = value["emission_group_id"]
                    if shot_number not in reference_group_shots[group]:
                        reference_group_shots[group].append(shot_number)
                    for unit_id in value.get("source", {}).get(
                            "semantic_unit_ids") or []:
                        if shot_number not in reference_unit_shots[str(unit_id)]:
                            reference_unit_shots[str(unit_id)].append(shot_number)
    if shot_number:
        for value in normalize_attachments(global_attachments):
            if value["enabled"] and value["kind"] == "reference":
                reference_group_shots[value["emission_group_id"]] = list(
                    range(1, shot_number + 1))
                for unit_id in value.get("source", {}).get(
                        "semantic_unit_ids") or []:
                    reference_unit_shots[str(unit_id)] = list(
                        range(1, shot_number + 1))
    context["reference_group_shots"] = dict(reference_group_shots)
    context["reference_unit_shots"] = dict(reference_unit_shots)
    if isinstance(context.get("ordinal_manifest"), dict):
        # `shots` is compiler-owned. A custom format declaring a physical
        # population with that same `ordinal_key` would have its ordinals
        # silently overwritten here. Detect the DECLARATION rather than the
        # key's presence: the key is also present on any re-entry with a reused
        # context, and refusing to write there would break `@shot` outright.
        claimed = any(str(declaration.get("ordinal_key") or "") == SHOT_ORDINAL_KEY
                      for declaration in physical_population_declarations(
                          context.get("profile") or {}))
        if claimed:
            context.setdefault("_reserved_key_conflicts", []).append(SHOT_ORDINAL_KEY)
        else:
            context["ordinal_manifest"][SHOT_ORDINAL_KEY] = shot_ordinals
    emitted_groups = {}
    # Same resolved text, different owner key. Definition/retention dedupe is
    # per semantic unit OR physical slot, so one member's Library prose reaching
    # a compile through both an identity chip and a physical chip produces two
    # owners by construction and passes the dedupe in silence. Advisory, not
    # blocking: emitting a subject and its source picture is legitimate, the
    # author just cannot see the repetition from either chip.
    emitted_group_text = {}
    unresolved_prompt_tokens = set()

    def resolve_attachment_tokens(value, attachment):
        declarations = prompt_token_declarations(resolved_profile)
        duplicate_slots = (context.get("setup_manifest", {}).get(
            "duplicate_member_slots") or {})
        for reference in prompt_tokens.references(value, declarations):
            declaration = declarations.get(reference["kind"]) or {}
            source_id = reference["source_id"]
            slots = [slot for slot in duplicate_slots.get(source_id) or []
                     if isinstance(slot, dict)
                     and str(slot.get("population") or "") == str(
                         declaration.get("population") or "")]
            if declaration.get("physical") is True and len(slots) > 1:
                labels = [
                    f"{slot.get('label') or slot.get('slot_id') or 'slot'}"
                    f" ({slot.get('lane_id')})" if slot.get("lane_id") else
                    str(slot.get("label") or slot.get("slot_id") or "slot")
                    for slot in slots if isinstance(slot, dict)
                ]
                errors.append({
                    "code": "ambiguous_physical_handle",
                    "attachment_id": attachment["attachment_id"],
                    "source_id": source_id,
                    "message": (f"Physical reference {source_id!r} occupies multiple "
                                f"setup slots: {', '.join(labels)}. Choose a specific "
                                "slot or remove the duplicate staging."),
                })
        resolved, unresolved_ids = prompt_tokens.resolve(
            value, context.get("ordinal_manifest") or {},
            context.get("unit_source_labels") or {}, declarations)
        for source_id in unresolved_ids:
            diagnostic_key = (attachment["attachment_id"], source_id)
            if diagnostic_key in unresolved_prompt_tokens:
                continue
            unresolved_prompt_tokens.add(diagnostic_key)
            errors.append({
                "code": "unresolved_prompt_token",
                "attachment_id": attachment["attachment_id"],
                "prompt_token_id": source_id,
                "message": (f"Prompt token source {source_id!r} has no ordinal "
                            "in the effective conditioning setup; rebind it."),
            })
        return resolved

    def set_projection_state(projection, state, text="", reason=""):
        if projection is None:
            return
        projection["state"] = state
        projection["text"] = str(text or "")
        if projection.get("_scope_inline") and state in {"emitted", "empty"}:
            projection["state_reason"] = (
                "Section-scope chips have no caret anchor; this is placed after "
                "section-prefix contributions and before authored text.")
        elif reason:
            projection["state_reason"] = reason

    def render(attachment, capability, *, origin, channel, projection=None):
        capability = capability or _default_capability(attachment, resolved_profile)
        capability_kind = capability.get("kind") or capability.get("capability_id")
        # Mentions are authored placements and therefore emit at each placement;
        # definitions/summary/retention remain semantic once-per-owner output.
        if capability_kind == "mentions":
            identity_owner = attachment["attachment_id"]
        elif capability_kind == "derived_prompt" and attachment.get(
                "source", {}).get("reference_item_id"):
            identity_owner = ("reference_item", str(
                attachment["source"]["reference_item_id"]))
        elif is_h3_reference_profile and capability_kind == "summary":
            identity_owner = "minimax_h3_summary"
        else:
            identity_owner = attachment["emission_group_id"]
        render_context = {**context, "origin": origin, "channel_key": channel,
                          "profile": resolved_profile}
        if (attachment["kind"] == "reference"
                and reference_capability_errors(
                    attachment, capability, render_context)):
            set_projection_state(
                projection, "invalid_field",
                reason="This capability contains a value outside the Prompt Format vocabulary.")
            return ""
        if (attachment["kind"] == "reference"
                and capability_kind in {"definitions", "retention"}):
            # Definition and retention output is deduped one semantic unit or
            # physical slot at a time, so overlapping chip selections neither
            # repeat a line nor hide a contradiction behind a differing tuple.
            kept = []
            resolved_lines = []
            for owner, line in reference_capability_lines(
                    attachment, capability, render_context):
                # Resolve before owner-level dedupe so stable-id aliases that
                # name the same Subject/slot compare as the same emission.
                line = resolve_attachment_tokens(line, attachment)
                resolved_lines.append(line)
                line_identity = (capability_kind, owner, channel)
                previous = emitted_groups.get(line_identity)
                if previous is not None:
                    if previous != line:
                        errors.append({
                            "code": "conflicting_emission",
                            "attachment_id": attachment["attachment_id"],
                            "message": ("The same semantic emission resolved to "
                                        "conflicting text."),
                        })
                    continue
                text_identity = (capability_kind, channel, line)
                first_owner = emitted_group_text.get(text_identity)
                if first_owner is not None and first_owner != owner:
                    warnings.append({
                        "code": "duplicate_reference_emission",
                        "attachment_id": attachment["attachment_id"],
                        "message": ("The same Reference text already emitted "
                                    "for a different subject or slot in this "
                                    "channel; blank one chip's field to stop it "
                                    "repeating."),
                    })
                elif first_owner is None:
                    emitted_group_text[text_identity] = owner
                emitted_groups[line_identity] = line
                kept.append(line)
            value = "\n".join(kept)
            if len(value.encode("utf-8")) > MAX_ATTACHMENT_OUTPUT:
                errors.append({"code": "attachment_output_limit",
                               "attachment_id": attachment["attachment_id"],
                               "message": "An attachment emitted more than 16 KiB."})
                set_projection_state(
                    projection, "output_limit", reason=
                    "This capability exceeded the 16 KiB attachment output limit.")
                return ""
            if value:
                emissions.append({"attachment_id": attachment["attachment_id"],
                                  "emission_group_id": attachment["emission_group_id"],
                                  "capability_id": capability["capability_id"],
                                  "kind": attachment["kind"], "channel_key": channel,
                                  "origin": origin,
                                  "placement": capability.get("placement") or "inline",
                                  "text": value})
                set_projection_state(projection, "emitted", value,
                                     "This capability emitted resolved text.")
            elif resolved_lines:
                set_projection_state(
                    projection, "deduplicated", "\n".join(resolved_lines),
                    "Equivalent output already emitted for this channel.")
            else:
                set_projection_state(projection, "empty", reason=
                                     "This capability resolved to no text.")
            return value
        identity = (identity_owner, capability["capability_id"],
                    capability.get("kind"), channel)
        value = _render_generic(
            attachment, capability, render_context,
            speakers_by_attachment.get(attachment["attachment_id"], []))
        if attachment["kind"] in {"reference", "custom"}:
            # Custom enum substitution is already complete here. The token pass
            # remains a separate stable-id-only operation and cannot expose a
            # general formatter or arbitrary field interpolation path.
            value = resolve_attachment_tokens(value, attachment)
        if len(value.encode("utf-8")) > MAX_ATTACHMENT_OUTPUT:
            errors.append({"code": "attachment_output_limit",
                           "attachment_id": attachment["attachment_id"],
                           "message": "An attachment emitted more than 16 KiB."})
            set_projection_state(
                projection, "output_limit", reason=
                "This capability exceeded the 16 KiB attachment output limit.")
            return ""
        prior = emitted_groups.get(identity)
        if prior is not None:
            if prior != value:
                errors.append({"code": "conflicting_emission",
                               "attachment_id": attachment["attachment_id"],
                               "message": "The same semantic emission resolved to conflicting text."})
            set_projection_state(
                projection, "deduplicated", value,
                "Equivalent output already emitted for this channel.")
            return ""
        emitted_groups[identity] = value
        if value:
            emissions.append({"attachment_id": attachment["attachment_id"],
                              "emission_group_id": attachment["emission_group_id"],
                              "capability_id": capability["capability_id"],
                              "kind": attachment["kind"], "channel_key": channel,
                              "origin": origin,
                              "placement": capability.get("placement") or "inline",
                              "text": value})
            set_projection_state(projection, "emitted", value,
                                 "This capability emitted resolved text.")
        else:
            set_projection_state(projection, "empty", reason=
                                 "This capability resolved to no text.")
        return value

    # Prompt links read local authored content and are resolved before other
    # attachment emission.  Earlier-only edges form a DAG by construction.
    by_prompt_id = {str(section.get("prompt_id") or ""): section
                    for section in raw_sections}
    ordered_sections = sorted(raw_sections, key=lambda value: (
        int(value.get("start_frame", 0)), str(value.get("prompt_id") or "")))
    section_positions = {str(section.get("prompt_id") or ""): index
                         for index, section in enumerate(ordered_sections)}
    link_cache = {}
    empty_link_diagnostics = set()
    # Build presence from the actual expanded output, in chronological order.
    # Earlier-only Prompt Links guarantee that every possible source has been
    # rendered before its consumer.  Pre-seeding this from plain-text mirrors
    # missed attachment-only sources (the private selection sentinel is not
    # authored output), so a link-exportable anchor was emitted once in its
    # source section and again as the consumer fallback.
    present_origins = set()
    link_fallbacks_emitted = set()

    def warn_empty_link(prompt_id, channel_key, attachment_id):
        diagnostic_key = (prompt_id, channel_key, attachment_id)
        if diagnostic_key in empty_link_diagnostics:
            return
        empty_link_diagnostics.add(diagnostic_key)
        warnings.append({"code": "empty_prompt_link",
                         "attachment_id": attachment_id,
                         "message":
                         f"Prompt Link source {prompt_id!r}/{channel_key!r} is empty."})

    def local_link_text(prompt_id, channel_key, consumer_index, trail=(),
                        diagnostic_attachment_id=""):
        cache_key = (prompt_id, channel_key)
        source = by_prompt_id.get(prompt_id)
        if source is None:
            errors.append({"code": "broken_prompt_link",
                           "attachment_id": diagnostic_attachment_id,
                           "message":
                           f"Prompt Link source {prompt_id!r} no longer exists."})
            return ""
        source_index = section_positions.get(prompt_id, -1)
        if source_index >= consumer_index or cache_key in trail:
            errors.append({"code": "invalid_prompt_link_order",
                           "attachment_id": diagnostic_attachment_id,
                           "message":
                           "Prompt Links must target an earlier section in the same scene."})
            return ""
        origin_key = (prompt_id, channel_key)
        if origin_key in present_origins or origin_key in link_fallbacks_emitted:
            return ""
        if cache_key in link_cache:
            value = link_cache[cache_key]
            if not value:
                warn_empty_link(prompt_id, channel_key, diagnostic_attachment_id)
            return value
        if source.get("muted"):
            return ""
        attachment_by_id = _attachment_map(source.get("attachments"))

        def render_link_anchor(attachment, capability_id, _anchor_node_id=None):
            if attachment["kind"] == "prompt_link" and attachment.get("link_exportable"):
                target = str(attachment["source"].get("prompt_id") or "")
                target_channel = str(attachment["source"].get("channel_key") or channel_key)
                return local_link_text(
                    target, target_channel, source_index, trail + (cache_key,),
                    attachment["attachment_id"])
            if attachment.get("link_exportable"):
                capability = next((cap for cap in _capabilities(attachment, resolved_profile)
                                   if cap["capability_id"] == capability_id), None)
                if capability is None and attachment.get("capabilities"):
                    return ""
                return _render_generic(attachment, capability or _default_capability(
                    attachment, resolved_profile), context,
                    speakers_by_attachment.get(attachment["attachment_id"], []))
            return ""

        scope_prefixes = []
        for attachment in attachment_by_id.values():
            if (attachment["kind"] != "prompt_link_scope"
                    or not attachment.get("link_exportable")
                    or not attachment.get("enabled", True)):
                continue
            link_source = attachment.get("source") or {}
            selected_channels = [str(value) for value in
                                 link_source.get("channel_keys") or [] if str(value)]
            if not selected_channels and link_source.get("channel_key"):
                selected_channels = [str(link_source.get("channel_key"))]
            if selected_channels and channel_key not in selected_channels:
                continue
            # The transitive path resolves a link THROUGH another link, and it
            # reads the same per-channel suppression the direct path does. It
            # previously consulted only chip-level `enabled`, so a channel muted
            # on the direct path reappeared the moment a later section chained
            # through it.
            if not _scope_link_capability(
                    attachment, resolved_profile, channel_key).get("enabled", True):
                continue
            scope_prefixes.append(local_link_text(
                str(link_source.get("prompt_id") or ""), channel_key,
                source_index, trail + (cache_key,), attachment["attachment_id"]))
        document_value = _document_render(
            source["channel_docs"].get(channel_key), attachment_by_id,
            render_link_anchor)
        value = _join_emissions(
            scope_prefixes + [document_value],
            resolved_profile.get("separators", {}).get("attachment", " "))
        if not value:
            warn_empty_link(prompt_id, channel_key, diagnostic_attachment_id)
        link_cache[cache_key] = value
        if value:
            link_fallbacks_emitted.add(origin_key)
        return value

    expanded_sections = []
    attachment_channel_routes = defaultdict(dict)
    attachment_capability_projections = []
    projection_rows = {}
    projection_discovery = 0

    def record_attachment_route(attachment, channel, placement):
        attachment_id = str(attachment.get("attachment_id") or "")
        channel = str(channel or "")
        if attachment_id and channel:
            routes = attachment_channel_routes[attachment_id].setdefault(channel, [])
            placement = str(placement or "section_prefix")
            if placement not in routes:
                routes.append(placement)

    def record_capability_projection(attachment, capability, channel, origin,
                                     anchor_node_id="scope"):
        nonlocal projection_discovery
        attachment_id = str(attachment.get("attachment_id") or "")
        channel = str(channel or "")
        capability_id = str(capability.get("capability_id") or "")
        anchor_identity = str(anchor_node_id or "scope")
        identity = (str(origin or ""), attachment_id, capability_id,
                    channel, anchor_identity)
        if not attachment_id or not capability_id or not channel:
            return None
        if identity in projection_rows:
            return projection_rows[identity]
        declared = str(capability.get("placement") or "section_prefix")
        effective = declared if declared in PLACEMENT_PHASES else "section_prefix"
        disabled = (not attachment.get("enabled", True)
                    or not capability.get("enabled", True))
        reason = ("This capability is disabled and contributes no text."
                  if disabled else "This capability resolved to no text.")
        if anchor_identity == "scope" and declared == "inline" and not disabled:
            reason = ("Section-scope chips have no caret anchor; this is placed after "
                      "section-prefix contributions and before authored text.")
        row = {
            "attachment_id": attachment_id,
            "emission_group_id": str(attachment.get("emission_group_id") or attachment_id),
            "attachment_kind": str(attachment.get("kind") or "custom"),
            "capability_id": capability_id,
            "capability_kind": str(capability.get("kind") or capability_id),
            "channel_key": channel,
            "declared_placement": declared,
            "effective_phase": effective,
            "region": ("after" if PLACEMENT_PHASES.index(effective)
                       > PLACEMENT_PHASES.index("inline") else "before"),
            "origin": str(origin or ""),
            "order": 0,
            "state": "disabled" if disabled else "empty",
            "state_reason": reason,
            "text": "",
            "rendered_at_anchor": False,
            "_anchor_node_id": anchor_identity,
            "_discovery": projection_discovery,
            "_scope_inline": anchor_identity == "scope" and declared == "inline",
        }
        projection_discovery += 1
        projection_rows[identity] = row
        attachment_capability_projections.append(row)
        return row

    for section in selected_sections:
        section_index = section_positions.get(
            str(section.get("prompt_id") or ""), 0)
        attachment_by_id = _attachment_map(section.get("attachments"))
        mirrors = {}
        phase_parts = defaultdict(lambda: defaultdict(list))
        scope_link_prefixes = defaultdict(list)

        def anchor_renderer(attachment, capability_id, channel_key, anchor_node_id):
            if attachment["kind"] == "prompt_link":
                capabilities = _capabilities(attachment, resolved_profile)
                if capability_id:
                    capabilities = [cap for cap in capabilities
                                    if cap["capability_id"] == capability_id]
                if not capabilities and attachment.get("capabilities"):
                    return ""
                capability = capabilities[0] if capabilities else _default_capability(
                    attachment, resolved_profile)
                route = _route_for(
                    attachment, capability, resolved_profile, channel_key)
                projection = record_capability_projection(
                    attachment, capability, route,
                    section.get("prompt_id", ""), anchor_node_id)
                if projection is not None:
                    projection["rendered_at_anchor"] = anchor_node_id != "scope"
                if not capability.get("enabled", True):
                    return ""
                target = str(attachment["source"].get("prompt_id") or "")
                target_channel = str(attachment["source"].get("channel_key") or channel_key)
                value = local_link_text(
                    target, target_channel, section_index,
                    diagnostic_attachment_id=attachment["attachment_id"])
                set_projection_state(
                    projection, "emitted" if value else "empty", value,
                    "Prompt Link emitted its earlier source." if value else
                    "Prompt Link resolved to no source text.")
                return value
            capabilities = _capabilities(attachment, resolved_profile)
            if capability_id:
                capabilities = [cap for cap in capabilities
                                if cap["capability_id"] == capability_id]
            if not capabilities:
                if attachment.get("capabilities"):
                    return ""
                capabilities = [_default_capability(attachment, resolved_profile)]
            inline_values = []
            for capability in capabilities:
                route = _route_for(attachment, capability, resolved_profile, channel_key)
                projection = record_capability_projection(
                    attachment, capability, route,
                    section.get("prompt_id", ""), anchor_node_id)
                if not capability.get("enabled", True):
                    continue
                record_attachment_route(attachment, route, capability["placement"])
                if route == channel_key and capability["placement"] == "inline":
                    if projection is not None:
                        projection["rendered_at_anchor"] = anchor_node_id != "scope"
                    inline_values.append(render(
                        attachment, capability, origin=section.get("prompt_id", ""),
                        channel=channel_key, projection=projection))
                else:
                    phase_parts[route][capability["placement"]].append(
                        (attachment, capability, projection))
            return _join_emissions(inline_values,
                                   resolved_profile.get("separators", {}).get(
                                       "attachment", " "))

        anchored = {node.get("attachment_id")
                    for document in section["channel_docs"].values()
                    for node in document.get("nodes", [])
                    if node.get("type") == "attachment"}
        for attachment in attachment_by_id.values():
            if not attachment["enabled"] or attachment["kind"] != "prompt_link_scope":
                continue
            if attachment["attachment_id"] in anchored:
                errors.append({
                    "code": "anchored_scope_only_attachment",
                    "attachment_id": attachment["attachment_id"],
                    "message": ("A section-scope Prompt Link cannot be anchored in "
                                "authored text; unlink, copy, or move it to the scope row."),
                })
                continue
            source = attachment.get("source") or {}
            target = str(source.get("prompt_id") or "")
            selected_channels = [str(value) for value in
                                 source.get("channel_keys") or [] if str(value)]
            if not selected_channels:
                selected_channels = ([str(source.get("channel_key"))]
                                     if source.get("channel_key") else list(keys))
            for target_channel in dict.fromkeys(selected_channels):
                # Per channel, so one channel can be muted while its siblings
                # keep emitting; the projection row carries the same per-channel
                # id, which is what the browser's suppression control writes to.
                capability = _scope_link_capability(
                    attachment, resolved_profile, target_channel)
                projection = record_capability_projection(
                    attachment, capability, target_channel,
                    section.get("prompt_id", ""), "scope")
                if target_channel not in keys:
                    set_projection_state(
                        projection, "invalid_route", reason=
                        f"Route {target_channel!r} is not in the active channel template.")
                    errors.append({
                        "code": "invalid_attachment_route",
                        "attachment_id": attachment["attachment_id"],
                        "message": f"Attachment route {target_channel!r} is not in the active template.",
                    })
                    continue
                if not capability.get("enabled", True):
                    continue
                value = local_link_text(
                    target, target_channel, section_index,
                    diagnostic_attachment_id=attachment["attachment_id"])
                set_projection_state(
                    projection, "emitted" if value else "empty", value,
                    "Section Prompt Link emitted its earlier source." if value else
                    "Section Prompt Link resolved to no source text.")
                if value:
                    scope_link_prefixes[target_channel].append(value)

        for key in keys:
            mirrors[key] = _document_render(
                section["channel_docs"].get(key), attachment_by_id,
                lambda attachment, capability_id, anchor_node_id, key=key:
                    anchor_renderer(attachment, capability_id, key, anchor_node_id))

        # Scope attachments are emitted by placement. Inline attachment nodes
        # already emitted above and are not emitted again here.
        shot = any(a["enabled"] and a["kind"] == "shot"
                   for a in attachment_by_id.values())
        timestamp = any(
            a["enabled"] and (
                (a["kind"] == "shot"
                 and bool((a.get("config") or {}).get("timestamp")))
                or (a["kind"] == "timestamp"
                    and bool((a.get("config") or {}).get("standalone"))))
            for a in attachment_by_id.values())
        for attachment in attachment_by_id.values():
            if (not attachment["enabled"]
                    or attachment["kind"] not in INLINE_ONLY_KINDS
                    or attachment["attachment_id"] in anchored):
                continue
            # A scope-row Prompt Link resolves nothing (only inline anchors do)
            # and a scope-row Vocal Event compiles as a prefix, losing its place
            # in the spoken order.  Legacy rows stay visible and block instead.
            errors.append({
                "code": "unanchored_inline_attachment",
                "attachment_id": attachment["attachment_id"],
                "message": (f"A {attachment['kind'].replace('_', ' ')} chip must be "
                            "inserted inline in the prompt text; this one has no "
                            "inline anchor. Re-insert or remove it."),
            })
        for attachment in attachment_by_id.values():
            if attachment["enabled"] and attachment["kind"] == "shot":
                for capability in _capabilities(attachment, resolved_profile):
                    route = _route_for(
                        attachment, capability, resolved_profile,
                        keys[0] if keys else "visual")
                    projection = record_capability_projection(
                        attachment, capability, route,
                        section.get("prompt_id", ""))
                    set_projection_state(
                        projection, "marker", reason=
                        "This marker is composed by the prompt section composer.")
            if (attachment["enabled"] and attachment["kind"] == "timestamp"
                    and bool((attachment.get("config") or {}).get("standalone"))):
                for capability in _capabilities(attachment, resolved_profile):
                    route = _route_for(
                        attachment, capability, resolved_profile,
                        keys[0] if keys else "visual")
                    projection = record_capability_projection(
                        attachment, capability, route,
                        section.get("prompt_id", ""))
                    if capability["enabled"]:
                        record_attachment_route(
                            attachment, route, capability["placement"])
                    set_projection_state(
                        projection, "marker", reason=
                        "This marker is composed by the prompt section composer.")
            if (not attachment["enabled"] or attachment["attachment_id"] in anchored
                    or attachment["kind"] in {"shot", "timestamp"}
                    or attachment["kind"] in INLINE_ONLY_KINDS
                    or attachment["kind"] in SCOPE_ONLY_KINDS):
                continue
            for capability in _capabilities(attachment, resolved_profile):
                route = _route_for(attachment, capability, resolved_profile,
                                   keys[0] if keys else "visual")
                projection = record_capability_projection(
                    attachment, capability, route, section.get("prompt_id", ""))
                if not capability["enabled"]:
                    continue
                record_attachment_route(attachment, route, capability["placement"])
                phase_parts[route][capability["placement"]].append(
                    (attachment, capability, projection))
        for key in dict.fromkeys([*scope_link_prefixes, *phase_parts]):
            phases = phase_parts[key]
            if key not in mirrors:
                attachment_ids = {a["attachment_id"]
                                  for values in phases.values()
                                  for a, _capability, _projection in values}
                for values in phases.values():
                    for _attachment, _capability, projection in values:
                        set_projection_state(
                            projection, "invalid_route", reason=
                            f"Route {key!r} is not in the active channel template.")
                for attachment_id in sorted(attachment_ids):
                    errors.append({"code": "invalid_attachment_route",
                                   "attachment_id": attachment_id,
                                   "message":
                                   f"Attachment route {key!r} is not in the active template."})
                continue
            attachment_separator = resolved_profile.get("separators", {}).get(
                "attachment", " ")
            prefixes = [(value, attachment_separator)
                        for value in scope_link_prefixes.get(key) or []]
            suffixes = []
            for phase in PLACEMENT_PHASES[:PLACEMENT_PHASES.index("inline") + 1]:
                prefixes.extend(
                    (render(a, c, origin=section.get("prompt_id", ""), channel=key,
                            projection=projection),
                     declared_capability_separator(c, resolved_profile,
                                                   attachment_separator))
                    for a, c, projection in phases.get(phase, []))
            for phase in PLACEMENT_PHASES[PLACEMENT_PHASES.index("inline") + 1:]:
                suffixes.extend(
                    (render(a, c, origin=section.get("prompt_id", ""), channel=key,
                            projection=projection),
                     declared_capability_separator(c, resolved_profile,
                                                   attachment_separator))
                    for a, c, projection in phases.get(phase, []))
            mirrors[key] = _join_declared_emissions(
                prefixes + [(mirrors[key], attachment_separator)] + suffixes,
                attachment_separator)
        prompt_id = str(section.get("prompt_id") or "")
        for key, value in mirrors.items():
            if str(value or "").strip():
                present_origins.add((prompt_id, str(key)))
        expanded_sections.append({
            "prompt_id": section.get("prompt_id", ""),
            "start_frame": section.get("start_frame", 0),
            "end_frame": section.get("end_frame", 0),
            "muted": bool(section.get("muted", False)),
            "channels": mirrors,
            "_opens_shot": shot,
            "_shot_timestamp": timestamp,
            "global_channel_exceptions": section.get("global_channel_exceptions", []),
        })

    # Global scope emissions prepend to their routed channel documents.
    global_by_id = _attachment_map(global_attachments)
    global_anchored = {node.get("attachment_id")
                       for document in global_docs.values()
                       for node in document.get("nodes", [])
                       if node.get("type") == "attachment"}
    global_phase_parts = defaultdict(lambda: defaultdict(list))

    def global_anchor_renderer(attachment, capability_id, channel_key, anchor_node_id):
        capabilities = _capabilities(attachment, resolved_profile)
        if capability_id:
            capabilities = [cap for cap in capabilities
                            if cap["capability_id"] == capability_id]
        if not capabilities:
            if attachment.get("capabilities"):
                return ""
            capabilities = [_default_capability(attachment, resolved_profile)]
        inline_values = []
        for capability in capabilities:
            route = _route_for(attachment, capability, resolved_profile, channel_key)
            projection = record_capability_projection(
                attachment, capability, route, "global", anchor_node_id)
            if not capability.get("enabled", True):
                continue
            record_attachment_route(attachment, route, capability["placement"])
            if route == channel_key and capability["placement"] == "inline":
                inline_values.append(render(attachment, capability,
                                            origin="global", channel=channel_key,
                                            projection=projection))
            else:
                global_phase_parts[route][capability["placement"]].append(
                    (attachment, capability, projection))
        return _join_emissions(inline_values,
                               resolved_profile.get("separators", {}).get(
                                   "attachment", " "))

    for key in keys:
        global_mirror[key] = _document_render(
            global_docs.get(key), global_by_id,
            lambda attachment, capability_id, anchor_node_id, key=key:
                global_anchor_renderer(
                    attachment, capability_id, key, anchor_node_id))
    for attachment in global_by_id.values():
        if not attachment["enabled"] or attachment["attachment_id"] in global_anchored:
            continue
        if attachment["kind"] in {"shot", "timestamp", "prompt_link",
                                   "prompt_link_scope"}:
            warnings.append({"code": "invalid_global_attachment",
                             "attachment_id": attachment["attachment_id"], "message":
                             f"{attachment['kind']} is section-scoped and was ignored globally."})
            continue
        for capability in _capabilities(attachment, resolved_profile):
            route = _route_for(attachment, capability, resolved_profile,
                               keys[0] if keys else "visual")
            projection = record_capability_projection(
                attachment, capability, route, "global")
            if not capability.get("enabled", True):
                continue
            record_attachment_route(attachment, route, capability["placement"])
            if route not in global_mirror:
                set_projection_state(
                    projection, "invalid_route", reason=
                    f"Route {route!r} is not in the active channel template.")
                errors.append({"code": "invalid_attachment_route",
                               "attachment_id": attachment["attachment_id"], "message":
                               f"Attachment route {route!r} is not in the active template."})
                continue
            global_phase_parts[route][capability["placement"]].append(
                (attachment, capability, projection))
    for route, phases in global_phase_parts.items():
        if route not in global_mirror:
            attachment_ids = {a["attachment_id"]
                              for values in phases.values()
                              for a, _capability, _projection in values}
            for values in phases.values():
                for _attachment, _capability, projection in values:
                    set_projection_state(
                        projection, "invalid_route", reason=
                        f"Route {route!r} is not in the active channel template.")
            for attachment_id in sorted(attachment_ids):
                errors.append({"code": "invalid_attachment_route",
                               "attachment_id": attachment_id,
                               "message":
                               f"Attachment route {route!r} is not in the active template."})
            continue
        global_attachment_separator = resolved_profile.get("separators", {}).get(
            "attachment", " ")
        prefixes, suffixes = [], []
        for phase in PLACEMENT_PHASES[:PLACEMENT_PHASES.index("inline") + 1]:
            prefixes.extend(
                (render(a, c, origin="global", channel=route, projection=projection),
                 declared_capability_separator(c, resolved_profile,
                                               global_attachment_separator))
                for a, c, projection in phases.get(phase, []))
        for phase in PLACEMENT_PHASES[PLACEMENT_PHASES.index("inline") + 1:]:
            suffixes.extend(
                (render(a, c, origin="global", channel=route, projection=projection),
                 declared_capability_separator(c, resolved_profile,
                                               global_attachment_separator))
                for a, c, projection in phases.get(phase, []))
        global_mirror[route] = _join_declared_emissions(
            prefixes + [(global_mirror[route], global_attachment_separator)] + suffixes,
            global_attachment_separator)

    final_prompt = prompt_payload.compose_range_prompt(
        prompt_payload.compose_section_text(global_mirror, labels_on=False),
        expanded_sections, window_start, window_end, labels_on=labels_on,
        delimiter=delimiter, boundary_threshold_pct=boundary_threshold_pct,
        template=resolved_template, fps=fps, global_channels=global_mirror)
    segments = prompt_payload.resolve_segments(
        expanded_sections, window_start, window_end,
        prompt_channel_templates.template_labels_on(resolved_template, labels_on),
        boundary_threshold_pct, resolved_template)
    shot_markers = prompt_payload.resolve_shot_markers(segments, fps)
    channel_outputs = {
        key: prompt_payload.join_segment_texts(
            [segment.get("channels", {}).get(key, "") for segment in segments],
            delimiter)
        for key in keys
    }

    setup_manifest = context.get("setup_manifest") or {}
    setup_value = setup_manifest.get("setup") or {}
    if is_h3_base_profile:
        task_mode = str(setup_value.get("task_mode") or "T2VA").upper()
        try:
            duration = max(0.0, (float(window_end) - float(window_start)) / float(fps))
        except (TypeError, ValueError, ZeroDivisionError):
            duration = 0.0
        shot_count = sum(1 for value in segments if value.get("_opens_shot"))
        final_shot = max(1, shot_count)
        instruction = ""
        if task_mode in {"I2VA", "FL2VA", "L2VA"} and shot_count == 0:
            warnings.append({"code": "missing_h3_shot_identity", "message":
                             f"{task_mode} Picture alignment is clearer with at least one authored Shot chip."})
        if task_mode == "I2VA":
            instruction = ("For the target video, at 0.00 seconds into the target video, "
                           "<Picture 1> (from [Shot 1]) is fully referenced.")
        elif task_mode == "FL2VA":
            instruction = ("How the reference pictures align with the target video — "
                           "Picture 1 (from Shot 1) aligns with the 0.00-second mark of the "
                           f"target video; Picture 2 (from Shot {final_shot}) aligns with the "
                           f"{duration:.2f}-second mark of the target video.")
        elif task_mode == "L2VA":
            instruction = ("How the reference pictures align with the target video — "
                           f"<Picture 1> (from [Shot {final_shot}]) aligns with the "
                           f"{duration:.2f}-second mark of the target video.")
        if instruction:
            final_prompt = f"{instruction}\n\n{final_prompt}" if final_prompt else instruction
        if task_mode not in {"T2VA", "I2VA", "FL2VA", "L2VA"}:
            errors.append({"code": "invalid_h3_task_mode",
                           "message": "MiniMax H3 task mode is invalid."})

    effective_values = {}
    for key in keys:
        values = []
        global_value = str(global_mirror.get(key) or "").strip()
        if global_value:
            values.append(global_value)
        values.extend(str(segment.get("channels", {}).get(key) or "").strip()
                      for segment in segments
                      if str(segment.get("channels", {}).get(key) or "").strip())
        effective_values[key] = _join_emissions(values, delimiter)

    # Empty authored channels are thin content, not unresolved state. Preserve
    # the MiniMax-specific code for its established UI copy; every other
    # template receives the general advisory.
    is_full_reference = (is_h3_reference_profile
                          and setup_value.get("mode") == "reference")
    for key in keys:
        if effective_values.get(key):
            continue
        if is_full_reference:
            warnings.append({"code": "missing_h3_reference_field",
                             "channel_key": key,
                             "message": f"MiniMax H3 Full Reference has no authored {key}."})
        else:
            warnings.append({"code": "empty_channel", "channel_key": key,
                             "message": f"Prompt channel {key} is empty and will be omitted."})

    # New Full Reference setups validate canonical form without turning thin
    # but fully resolved prose into a queue blocker. Legacy frozen envelopes
    # without setup authority stay readable through the compatibility path.
    if is_full_reference:
        summary = effective_values.get("summary", "")
        task_type_values = [choice["value"] for choice in declared_field_choices(
            declared_reference_field(resolved_profile, "summary", "task_types"))]
        # Declaration values are data, never regex fragments.
        task_pattern = "|".join(re.escape(value) for value in task_type_values)
        if (summary and task_pattern and not re.match(
                rf"^\[(?:{task_pattern})(?: \+ (?:{task_pattern}))*\]", summary)):
            warnings.append({"code": "invalid_h3_task_prefix", "message":
                             "Full Reference summary should begin with canonical task types."})
        retention_emissions = [value for value in emissions
                               if value.get("kind") == "reference"
                               and value.get("capability_id") == "retention"]
        for emission in retention_emissions:
            for line in str(emission.get("text") or "").splitlines():
                if line.strip() and " - " not in line:
                    warnings.append({
                        "code": "missing_h3_retention_detail",
                        "attachment_id": emission.get("attachment_id", ""),
                        "message": "MiniMax H3 retention markers require authored detail after ' - '.",
                    })
                    break

    for validator in resolved_profile.get("validators") or []:
        if not isinstance(validator, dict):
            continue
        kind = validator.get("kind")
        severity = validator.get("severity") or "error"
        target = warnings if severity == "warning" else errors
        channel_key = str(validator.get("channel_key") or "")
        channel_value = _join_emissions(
            [global_mirror.get(channel_key, ""), channel_outputs.get(channel_key, "")],
            delimiter)
        failed = False
        default_message = "Prompt Context profile validation failed."
        if kind == "required_channel":
            failed = not channel_value.strip()
            default_message = f"Profile requires authored content in {channel_key}."
        elif kind == "max_channel_chars":
            failed = len(channel_value) > int(validator.get("limit") or 0)
            default_message = (f"Profile limits {channel_key} to "
                               f"{validator.get('limit')} characters.")
        elif kind == "max_final_chars":
            failed = len(final_prompt) > int(validator.get("limit") or 0)
            default_message = (f"Profile limits the compiled prompt to "
                               f"{validator.get('limit')} characters.")
        elif kind == "require_attachment_kind":
            attachment_kind = str(validator.get("attachment_kind") or "")
            failed = not any(value["enabled"] and value["kind"] == attachment_kind
                             for value in all_attachments)
            default_message = f"Profile requires a {attachment_kind} Context chip."
        if failed:
            target.append({"code": f"profile_{kind}",
                           "message": validator.get("message") or default_message})

    if len(final_prompt.encode("utf-8")) > MAX_COMPILED_PROMPT:
        errors.append({"code": "compiled_prompt_limit", "message":
                       "The compiled prompt exceeds 256 KiB."})
    # Prompt Relay's global input is a raw always-on prefix, historically
    # label-free even when local segments use labelled channels.
    relay_global = prompt_payload.compose_section_text(
        global_mirror, labels_on=False, template=resolved_template)
    relay_manifest = prompt_payload.build_relay_payload(relay_global, segments)
    previews = defaultdict(list)
    channel_previews = defaultdict(lambda: defaultdict(list))
    for emission in emissions:
        previews[emission["attachment_id"]].append(emission["text"])
        channel_previews[emission["attachment_id"]][emission["channel_key"]].append(
            emission["text"])
    # Shot/Time markers are composed by prompt_payload after channel attachment
    # emissions, so a standalone Time has no ordinary emission to preview. Give
    # its durable chip the exact window-local marker the composer resolved.
    segments_by_prompt_id = {
        str(segment.get("prompt_id") or ""): segment for segment in segments
    }
    markers_by_prompt_id = {
        str(segment.get("prompt_id") or ""): marker
        for segment, marker in zip(segments, shot_markers)
    }
    for section in selected_sections:
        segment = segments_by_prompt_id.get(str(section.get("prompt_id") or ""))
        if segment is None:
            continue
        timecode = prompt_channel_templates.format_shot_timecode(
            segment.get("start", 0), fps)
        if not timecode:
            continue
        for attachment in section.get("attachments") or []:
            if (not attachment.get("enabled", True)
                    or attachment.get("kind") != "timestamp"
                    or not bool((attachment.get("config") or {}).get("standalone"))):
                continue
            attachment_id = str(attachment.get("attachment_id") or "")
            routes = attachment_channel_routes.get(attachment_id) or {}
            marker = f"At {timecode},"
            previews[attachment_id].append(marker)
            for channel_key in routes:
                channel_previews[attachment_id][channel_key].append(marker)

    for section in selected_sections:
        origin = str(section.get("prompt_id") or "")
        marker = markers_by_prompt_id.get(origin, "")
        if not marker:
            continue
        for attachment in section.get("attachments") or []:
            kind = str(attachment.get("kind") or "")
            if kind not in {"shot", "timestamp"}:
                continue
            if (kind == "timestamp"
                    and not bool((attachment.get("config") or {}).get("standalone"))):
                continue
            marker_text = marker
            if kind == "timestamp" and " At " in marker:
                marker_text = f"At {marker.split(' At ', 1)[1]}"
            for projection in attachment_capability_projections:
                if (projection["origin"] == origin
                        and projection["attachment_id"]
                        == str(attachment.get("attachment_id") or "")
                        and projection["state"] == "marker"):
                    set_projection_state(
                        projection, "marker", marker_text,
                        "This marker is composed by the prompt section composer.")

    emitted_by_group_channel = defaultdict(set)
    for emission in emissions:
        emitted_by_group_channel[
            (str(emission.get("emission_group_id") or ""),
             str(emission.get("channel_key") or ""))
        ].add(str(emission.get("attachment_id") or ""))
    error_attachment_ids = {
        str(error.get("attachment_id") or "") for error in errors
        if str(error.get("attachment_id") or "")
    }
    for projection in attachment_capability_projections:
        if (projection["state"] in {"empty", "deduplicated"}
                and projection["attachment_id"] in error_attachment_ids):
            set_projection_state(
                projection, "unresolved", projection.get("text", ""),
                "A blocking diagnostic prevented this capability from resolving.")
            continue
        linked_emitters = emitted_by_group_channel.get((
            projection["emission_group_id"], projection["channel_key"]), set())
        if (projection["state"] in {"empty", "deduplicated"}
                and any(value != projection["attachment_id"]
                        for value in linked_emitters)):
            set_projection_state(
                projection, "linked_elsewhere", projection.get("text", ""),
                "Equivalent output was emitted by a linked chip elsewhere.")

    projection_orders = defaultdict(int)
    for projection in sorted(
            attachment_capability_projections,
            key=lambda value: value.get("_discovery", 0)):
        order_key = (projection["origin"], projection["channel_key"])
        projection["order"] = projection_orders[order_key]
        projection_orders[order_key] += 1
        projection.pop("_anchor_node_id", None)
        projection.pop("_discovery", None)
        projection.pop("_scope_inline", None)
    # Built HERE, from the context this compile enriched, and never from a
    # dict assembled by a caller. `_reference_capability_parts` reads
    # `semantic_units_by_id` and `profile`, both injected above; a caller that
    # enumerated context keys by hand got neither and produced empty plans that
    # looked like successful conversions. Enumeration is the bug, so this does
    # not enumerate.
    convert_plan = None
    if convert_plan_for is not None:
        located = _locate_capability(convert_plan_for, global_attachments, sections)
        convert_plan = (convert_capability_plan(located[0], located[1], context)
                        if located else
                        {"lines": [], "frozen": [],
                         "refused": "That capability is not staged in this scene."})

    result = {
        "format": FORMAT_VERSION,
        "prompt": final_prompt,
        "channels": channel_outputs,
        "segments": segments,
        "relay": relay_manifest,
        "window": {"start_frame": int(window_start), "end_frame": int(window_end),
                   "fps": float(fps)},
        "emissions": emissions,
        **({"convert_plan": convert_plan} if convert_plan is not None else {}),
        "attachment_previews": {key: "\n".join(value)
                                for key, value in previews.items()},
        "attachment_channel_previews": {
            attachment_id: {channel_key: "\n".join(values)
                            for channel_key, values in channels.items()}
            for attachment_id, channels in channel_previews.items()
        },
        "attachment_channel_routes": {
            attachment_id: {
                channel_key: (ordered[0] if len(ordered) == 1 else ordered)
                for channel_key, values in routes.items()
                for ordered in [[phase for phase in PLACEMENT_PHASES if phase in values]]
            }
            for attachment_id, routes in attachment_channel_routes.items()
        },
        "attachment_capability_projections": copy.deepcopy(
            attachment_capability_projections),
        # This is the compiler's effective-window speaker domain after section
        # holding, overlap resolution, clipping and boundary threshold. The UI
        # consumes it rather than approximating eligibility from authored bars.
        "managed_speaker_subject_ids": list(speaker_order),
        "setup_manifest": copy.deepcopy(context.get("setup_manifest") or {}),
        "ordinal_manifest": copy.deepcopy(context.get("ordinal_manifest") or {}),
        "profile": copy.deepcopy(resolved_profile),
        "profile_hash": resolved_profile["content_hash"],
        "warnings": warnings,
        "errors": errors,
    }
    result["content_hash"] = content_hash({
        "prompt": result["prompt"], "channels": result["channels"],
        "segments": result["segments"], "window": result["window"],
        "profile_hash": result["profile_hash"],
        "setup_manifest": result["setup_manifest"],
    })
    return result
