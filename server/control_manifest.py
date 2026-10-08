"""The Sonder Control Relay manifest: parsing, hygiene and the Relay census.

The Relay (``nodes/control_relay.py``) carries one hidden ``manifest`` string.
Saved in a workflow it holds presentation only, keyed by ``binding_id``:

    {"v": 1,
     "bindings": {"<id>": {"label", "section", "group", "order", "advanced", "note"}},
     "notes": {"<id>": {"text", "group", "order"}},
     "groups": {"<name>": {"order"}}}

Its API export (``web/js/control_relay.js`` ``serializeValue``) adds, per binding,
``status``, ``targets: [{path, input}]`` and ``seed_mode``, plus ``suggestions``
(anchors without a manifest entry) and, when the frontend resolver failed,
``error``. The server never trusts an exported ``ok``: ``control_descriptors``
re-checks every target against the prompt itself.

Pure and standard-library only, so tests and any caller may import it freely.
"""

from __future__ import annotations

import json
import re
from typing import Any

RELAY_CLASS_TYPE = "SonderControlRelay"
MANIFEST_VERSION = 1

# Phase 0 P10 hygiene. The manifest travels inside every exported prompt, its
# embedded workflow and output metadata, so it stays small.
MAX_MANIFEST_BYTES = 128 * 1024
MAX_BINDINGS = 256
MAX_NOTES = 256
MAX_SUGGESTIONS = 256
MAX_TARGETS = 64
MAX_GROUPS = 256
# Nesting levels, counting the top object as 1. The export needs 5
# (manifest > bindings > entry > targets > target); 6 leaves one spare.
MAX_DEPTH = 6
LABEL_MAX = 120
NOTE_MAX = 4000
ORDER_LIMIT = 9999
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FORBIDDEN_KEYS = frozenset({"__proto__", "constructor", "prototype"})
SECTIONS = ("workflow", "editor")
EXPORT_STATUSES = frozenset({"ok", "missing", "duplicate", "linked", "no_widget", "unsupported"})
SEED_MODES = ("fixed", "increment", "decrement", "randomize")


class ManifestError(ValueError):
    """A manifest the Relay cannot use. The message is shown to the user."""


def empty_manifest() -> dict:
    return {"v": MANIFEST_VERSION, "bindings": {}, "notes": {}, "groups": {}, "suggestions": []}


def _check_shape(value: Any, depth: int = 1) -> None:
    if depth > MAX_DEPTH:
        raise ManifestError("The Controls manifest is nested too deeply.")
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_KEYS:
                raise ManifestError("The Controls manifest uses a reserved key.")
            _check_shape(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_shape(item, depth + 1)


def _text(value: Any, limit: int, what: str, *, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ManifestError(f"A Controls {what} must be text.")
    if len(value) > limit:
        raise ManifestError(f"A Controls {what} is longer than {limit} characters.")
    return value


def _order(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int) or not -ORDER_LIMIT <= value <= ORDER_LIMIT:
        raise ManifestError("A Controls order must be a whole number from -9999 to 9999.")
    return value


def _check_id(value: Any, what: str = "binding") -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ManifestError(f"A Controls {what} id is invalid.")
    return value


def _targets(value: Any) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_TARGETS:
        raise ManifestError("A Controls binding has invalid targets.")
    targets = []
    for target in value:
        if not isinstance(target, dict):
            raise ManifestError("A Controls binding has invalid targets.")
        path, name = target.get("path"), target.get("input")
        if not isinstance(path, str) or not path or len(path) > 256 or not isinstance(name, str) or not name or len(name) > 256:
            raise ManifestError("A Controls binding has invalid targets.")
        targets.append({"path": path, "input": name})
    return targets


def _binding(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ManifestError("A Controls binding must be an object.")
    section = raw.get("section", "workflow")
    if section not in SECTIONS:
        raise ManifestError("A Controls binding has an unknown section.")
    advanced = raw.get("advanced", False)
    if not isinstance(advanced, bool):
        raise ManifestError("A Controls advanced flag must be true or false.")
    entry = {
        "label": _text(raw.get("label"), LABEL_MAX, "label").strip(),
        "section": section,
        "group": _text(raw.get("group"), LABEL_MAX, "group").strip(),
        "order": _order(raw.get("order")),
        "advanced": advanced,
        "note": _text(raw.get("note"), NOTE_MAX, "note"),
    }
    # Export-only facts. A saved manifest has none of them.
    status = raw.get("status")
    if status is not None:
        entry["status"] = status if status in EXPORT_STATUSES else "missing"
    entry["targets"] = _targets(raw.get("targets"))
    mode = raw.get("seed_mode")
    entry["seed_mode"] = mode if mode in SEED_MODES else None
    return entry


def parse_manifest(text: Any) -> dict:
    """Parse and check a saved or exported manifest; raise ``ManifestError``."""
    if text is None or (isinstance(text, str) and not text.strip()):
        return empty_manifest()
    if not isinstance(text, str):
        raise ManifestError("The Controls manifest must be text.")
    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        raise ManifestError("The Controls manifest is too large (over 128 KiB). Remove some Controls or notes.")
    try:
        raw = json.loads(text)
    except RecursionError:
        raise ManifestError("The Controls manifest is nested too deeply.") from None
    except ValueError:
        raise ManifestError("The Controls manifest is not valid JSON.") from None
    if not isinstance(raw, dict):
        raise ManifestError("The Controls manifest must be an object.")
    _check_shape(raw)
    if raw.get("v", MANIFEST_VERSION) != MANIFEST_VERSION:
        raise ManifestError("This Controls manifest comes from a newer Sonder version.")

    bindings_raw = raw.get("bindings") or {}
    notes_raw = raw.get("notes") or {}
    groups_raw = raw.get("groups") or {}
    suggestions_raw = raw.get("suggestions") or []
    if not isinstance(bindings_raw, dict) or len(bindings_raw) > MAX_BINDINGS:
        raise ManifestError(f"The Controls manifest may hold at most {MAX_BINDINGS} Controls.")
    if not isinstance(notes_raw, dict) or len(notes_raw) > MAX_NOTES:
        raise ManifestError(f"The Controls manifest may hold at most {MAX_NOTES} notes.")
    if not isinstance(groups_raw, dict) or len(groups_raw) > MAX_GROUPS:
        raise ManifestError("The Controls manifest has too many groups.")
    if not isinstance(suggestions_raw, list) or len(suggestions_raw) > MAX_SUGGESTIONS:
        raise ManifestError("The Controls manifest has too many unlisted anchors.")

    manifest = empty_manifest()
    for binding_id, entry in bindings_raw.items():
        manifest["bindings"][_check_id(binding_id)] = _binding(entry)
    for note_id, entry in notes_raw.items():
        if not isinstance(entry, dict):
            raise ManifestError("A Controls note must be an object.")
        manifest["notes"][_check_id(note_id, "note")] = {
            "text": _text(entry.get("text"), NOTE_MAX, "note"),
            "group": _text(entry.get("group"), LABEL_MAX, "group").strip(),
            "order": _order(entry.get("order")),
        }
    for name, entry in groups_raw.items():
        _text(name, LABEL_MAX, "group")
        if not isinstance(entry, dict):
            raise ManifestError("A Controls group entry must be an object.")
        manifest["groups"][name] = {"order": _order(entry.get("order"))}
    for entry in suggestions_raw:
        if not isinstance(entry, dict):
            raise ManifestError("An unlisted anchor must be an object.")
        manifest["suggestions"].append({
            "binding_id": _check_id(entry.get("binding_id")),
            "targets": _targets(entry.get("targets")),
        })
    error = raw.get("error")
    if isinstance(error, str) and error:
        manifest["error"] = error[:500]
    return manifest


def find_relay(prompt: Any) -> dict:
    """The one usable Relay in an API prompt, or why there is none.

    Returns ``{status: "none"|"ok"|"error", key, manifest, error}``. A muted or
    bypassed Relay is absent from an export; only the canvas census sees it.
    """
    keys = [str(key) for key, node in (prompt.items() if isinstance(prompt, dict) else [])
            if isinstance(node, dict) and node.get("class_type") == RELAY_CLASS_TYPE]
    if not keys:
        return {"status": "none", "key": None, "manifest": empty_manifest(), "error": None}
    nested = [key for key in keys if ":" in key]
    if nested:
        return {"status": "error", "key": nested[0], "manifest": empty_manifest(),
                "error": "A Sonder Control Relay inside a subgraph is not supported. Move it to the main graph."}
    if len(keys) > 1:
        return {"status": "error", "key": keys[0], "manifest": empty_manifest(),
                "error": "The workflow has more than one Sonder Control Relay. Keep one."}
    key = keys[0]
    inputs = prompt[key].get("inputs") if isinstance(prompt[key].get("inputs"), dict) else {}
    try:
        manifest = parse_manifest(inputs.get("manifest"))
    except ManifestError as exc:
        return {"status": "error", "key": key, "manifest": empty_manifest(), "error": str(exc)}
    if manifest.get("error"):
        return {"status": "error", "key": key, "manifest": manifest,
                "error": "The Controls could not be read from the workflow: " + manifest["error"]}
    return {"status": "ok", "key": key, "manifest": manifest, "error": None}


def census_error(census: Any) -> str | None:
    """A canvas census problem the export cannot show (muted, bypassed, several)."""
    if not isinstance(census, dict):
        return None

    def count(name: str) -> int:
        value = census.get(name)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    if count("active") > 1:
        return "The workflow has more than one Sonder Control Relay. Keep one."
    if count("nested"):
        return "A Sonder Control Relay inside a subgraph is not supported. Move it to the main graph."
    if not count("active") and (count("muted") or count("bypassed")):
        return "The Sonder Control Relay is muted or bypassed, so its Controls are off."
    return None
