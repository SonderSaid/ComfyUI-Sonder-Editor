"""Stable-id prompt token grammar and compile-time ordinal resolution."""

from __future__ import annotations

import re


PROMPT_TOKEN_KINDS = {
    "subject": ("subjects", "Subject"),
    "picture": ("pictures", "Picture"),
    "video": ("videos", "Video"),
    "audio": ("audios", "Audio"),
}

_TOKEN_RE = re.compile(
    r"@(?P<kind>subject|picture|video|audio)\("
    r"(?P<source_id>[A-Za-z0-9][A-Za-z0-9._:-]*)\)"
)


def token(kind: str, source_id: str) -> str:
    """Return an authored token for a supported kind and stable source id."""
    normalized_kind = str(kind or "").lower()
    normalized_id = str(source_id or "").strip()
    if normalized_kind not in PROMPT_TOKEN_KINDS or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]*", normalized_id):
        return ""
    return f"@{normalized_kind}({normalized_id})"


def find(text) -> list[str]:
    """Return supported authored token spellings without resolving them."""
    return [match.group(0) for match in _TOKEN_RE.finditer(str(text or ""))]


def _unit_label_ordinal(kind: str, source_id: str, unit_source_labels) -> int | None:
    """Compatibility fallback for a semantic id with exactly one typed source."""
    _manifest_key, label = PROMPT_TOKEN_KINDS[kind]
    prefix = f"<{label} "
    ordinals = []
    for value in (unit_source_labels or {}).get(source_id) or []:
        text = str(value)
        if not text.startswith(prefix) or not text.endswith(">"):
            continue
        number = text[len(prefix):-1]
        if number.isdigit():
            ordinals.append(int(number))
    unique = sorted(set(ordinals))
    return unique[0] if len(unique) == 1 else None


def resolve(text, ordinal_manifest, unit_source_labels=None) -> tuple[str, list[str]]:
    """Resolve supported tokens without mutating the authored text or manifests.

    Unknown stable ids remain visible in the returned text and are reported in
    first-occurrence order so the compiler can attach blocking diagnostics to
    the owning attachment.
    """
    value = str(text or "")
    manifest = ordinal_manifest if isinstance(ordinal_manifest, dict) else {}
    unresolved = []

    def replace(match: re.Match) -> str:
        kind = match.group("kind")
        source_id = match.group("source_id")
        manifest_key, label = PROMPT_TOKEN_KINDS[kind]
        number = (manifest.get(manifest_key) or {}).get(source_id)
        if number is None and kind != "subject":
            number = _unit_label_ordinal(kind, source_id, unit_source_labels)
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            if source_id not in unresolved:
                unresolved.append(source_id)
            return match.group(0)
        return f"<{label} {number}>"

    return _TOKEN_RE.sub(replace, value), unresolved
