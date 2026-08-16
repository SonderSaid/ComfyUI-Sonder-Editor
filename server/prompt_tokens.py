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
    r"@(?P<kind>[a-z][a-z0-9_]{0,63})\("
    r"(?P<source_id>[A-Za-z0-9][A-Za-z0-9._:-]*)\)"
)


def _declarations(raw=None) -> dict:
    if raw is None:
        return {
            kind: {"manifest_key": manifest_key,
                   "label_template": f"<{label} {{n}}>",
                   "physical": kind != "subject"}
            for kind, (manifest_key, label) in PROMPT_TOKEN_KINDS.items()
        }
    return {str(kind): value for kind, value in raw.items()
            if isinstance(kind, str) and isinstance(value, dict)} \
        if isinstance(raw, dict) else {}


def token(kind: str, source_id: str, declarations=None) -> str:
    """Return an authored token for a supported kind and stable source id."""
    normalized_kind = str(kind or "").lower()
    normalized_id = str(source_id or "").strip()
    if normalized_kind not in _declarations(declarations) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]*", normalized_id):
        return ""
    return f"@{normalized_kind}({normalized_id})"


def references(text, declarations=None) -> list[dict]:
    supported = _declarations(declarations)
    return [{"token": match.group(0), "kind": match.group("kind"),
             "source_id": match.group("source_id")}
            for match in _TOKEN_RE.finditer(str(text or ""))
            if match.group("kind") in supported]


def find(text, declarations=None) -> list[str]:
    """Return supported authored token spellings without resolving them."""
    return [value["token"] for value in references(text, declarations)]


def _unit_label_ordinal(declaration, source_id: str, unit_source_labels) -> int | None:
    """Compatibility fallback for a semantic id with exactly one typed source."""
    template = str((declaration or {}).get("label_template") or "")
    prefix, suffix = template.split("{n}", 1) if "{n}" in template else ("", "")
    ordinals = []
    for value in (unit_source_labels or {}).get(source_id) or []:
        text = str(value)
        if not prefix or not text.startswith(prefix) or not text.endswith(suffix):
            continue
        number = text[len(prefix):len(text) - len(suffix) if suffix else None]
        if number.isdigit():
            ordinals.append(int(number))
    unique = sorted(set(ordinals))
    return unique[0] if len(unique) == 1 else None


def resolve(text, ordinal_manifest, unit_source_labels=None,
            declarations=None) -> tuple[str, list[str]]:
    """Resolve supported tokens without mutating the authored text or manifests.

    Unknown stable ids remain visible in the returned text and are reported in
    first-occurrence order so the compiler can attach blocking diagnostics to
    the owning attachment.
    """
    value = str(text or "")
    manifest = ordinal_manifest if isinstance(ordinal_manifest, dict) else {}
    declared = _declarations(declarations)
    unresolved = []

    def replace(match: re.Match) -> str:
        kind = match.group("kind")
        source_id = match.group("source_id")
        declaration = declared.get(kind)
        if declaration is None:
            return match.group(0)
        manifest_key = str(declaration.get("manifest_key") or "")
        label_template = str(declaration.get("label_template") or "")
        number = (manifest.get(manifest_key) or {}).get(source_id)
        if number is None and declaration.get("physical") is True:
            number = _unit_label_ordinal(declaration, source_id, unit_source_labels)
        try:
            number = int(number)
        except (TypeError, ValueError):
            number = 0
        if number <= 0:
            if source_id not in unresolved:
                unresolved.append(source_id)
            return match.group(0)
        try:
            return label_template.format(n=number)
        except (KeyError, ValueError, IndexError):
            return match.group(0)

    return _TOKEN_RE.sub(replace, value), unresolved
