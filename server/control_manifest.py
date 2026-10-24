"""The Sonder Control Relay manifest: parsing, hygiene and the Relay census.

The Relay (``nodes/control_relay.py``) carries one hidden ``manifest`` string.
Saved in a workflow it holds presentation only, keyed by ``binding_id``:

    {"v": 1,
     "bindings": {"<id>": {"label", "section", "group", "order", "advanced", "note"}},
     "notes": {"<id>": {"text", "group", "order"}},
     "groups": {"<name>": {"order"}},
     "execution": {"v": 1, "controls": {"<id>": {"label", "group", "order", "advanced", "note",
                                                "target": {"kind": "node"|"instance"|"group", "group_id"?}}}},
     "views": {"v": 1, "items": {"<id>": {"label", "order"}}}}

A node or instance target is located by ``node.properties.sonderExecution`` on a
root node (as a binding is by its anchor); a group by its root ``group.id``.

Its API export (``web/js/control_relay.js`` ``serializeValue``) adds, per binding,
``status``, ``targets: [{path, input}]``, ``seed_mode`` and ``participation``
(with ``inactive_reason``); per execution Control, ``status``, ``members`` and
``title``; plus ``suggestions``
(anchors without a manifest entry) and, when the frontend resolver failed,
``error``. The server never trusts an exported ``ok``: ``control_descriptors``
re-checks every target against the prompt itself.

Pure and standard-library only, so tests and any caller may import it freely.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from . import control_rows

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
MAX_PRESETS = 32
MAX_PRESET_TARGETS = 64
# Nesting levels, counting the top object as 1. A preset holding a row list
# needs 8 (manifest > presets > preset > members > member > rows > row >
# value), with no spare; the export needs 5. control_bindings.js mirrors it.
MAX_DEPTH = 8
LABEL_MAX = 120
NOTE_MAX = 4000
DESCRIPTION_MAX = 1000
ORDER_LIMIT = 9999
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FORBIDDEN_KEYS = frozenset({"__proto__", "constructor", "prototype"})
SECTIONS = ("workflow", "editor")
EXPORT_STATUSES = frozenset({"ok", "missing", "duplicate", "linked", "no_widget", "unsupported"})
SEED_MODES = ("fixed", "increment", "decrement", "randomize")
# A binding's exported participation: its target nodes run, are off (muted or
# bypassed themselves or under a muted or bypassed subgraph host), or both.
PARTICIPATIONS = frozenset({"active", "inactive", "partial"})
INACTIVE_MODES = ("muted", "bypassed")

# Execution Controls: a root node, a root subgraph instance or a root canvas
# group switched Enabled, Muted or Bypassed. Versioned apart from the manifest:
# an unknown version turns off this section only. web/js/control_bindings.js
# mirrors it.
EXECUTION_VERSION = 1
MAX_EXECUTION = 64
# Members one execution Control (a group) may switch, and the distinct node
# effects one preset may hold beside its other targets.
MAX_EXECUTION_MEMBERS = 64
EXECUTION_KINDS = ("node", "instance", "group")
EXECUTION_MODES = ("enabled", "muted", "bypassed")
EXECUTION_STATUSES = frozenset({"ok", "missing", "duplicate", "unsupported"})
# Why the canvas resolver refused a target, as a code the server words itself.
EXECUTION_PROBLEMS = frozenset({"infrastructure", "too_many", "empty", "kind", "virtual", "budget"})
# Root execution ids (litegraph node ids); a member is never inside a subgraph.
_ROOT_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Output Views: named inspection sources, one per output node. Versioned apart
# from the manifest like execution: another version turns off this section
# only. A View is located by ``node.properties.sonderViews = {rel: id}`` on a
# root node (``""``) or on the root subgraph instance holding the output
# (``rel``, its path inside the instance); the manifest holds presentation.
# web/js/control_bindings.js mirrors it.
VIEWS_VERSION = 1
MAX_VIEWS = 32
VIEW_STATUSES = frozenset({"ok", "missing", "duplicate", "kind", "unsupported"})
# An execution path: 1-8 ``:``-joined node ids (MAX_NESTING), the grammar
# ComfyUI gives an expanded subgraph node.
MAX_PATH_SEGMENTS = 8
_PATH_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}(?::[A-Za-z0-9_-]{1,64}){0,7}$")


def clean_path(value: Any) -> str | None:
    """An execution path (root id or ``host:inner:...``), or None."""
    if not isinstance(value, str) or not _PATH_RE.match(value):
        return None
    return None if any(part in FORBIDDEN_KEYS for part in value.split(":")) else value


class ManifestError(ValueError):
    """A manifest the Relay cannot use. The message is shown to the user."""


# Group names are keys in the manifest, the Sonder App sidecar (``group:<name>``)
# and change targets (``group:<name>:<op>``), so one rule serves every writer:
# stripped, 1-120 code points, not a prototype name, no ``:`` and no control
# characters. web/js/control_bindings.js mirrors it.
# Checked before stripping, with U+FEFF, so Python's strip() and JS trim()
# (whose whitespace sets differ only in these) strip a valid name alike.
_GROUP_FORBIDDEN_CHARS = re.compile(r"[:\x00-\x1f\x7f-\x9f﻿]")


def group_name_error(name: Any) -> str | None:
    """Why ``name`` cannot be a group name, or None. Writers refuse with it."""
    if not isinstance(name, str):
        return "A group name must be text."
    if _GROUP_FORBIDDEN_CHARS.search(name):
        return "A group name can't contain a colon or control characters."
    name = name.strip()
    if not name:
        return "A group needs a name."
    if len(name) > LABEL_MAX:
        return f"A group name must be at most {LABEL_MAX} characters."
    if name in FORBIDDEN_KEYS:
        return "This group name is reserved. Choose another."
    return None


def clean_group(value: Any) -> str:
    """A stored group read tolerantly: its stripped name, or ungrouped (``""``)
    when it breaks the name rule. A cosmetic problem never blocks Generate."""
    if not isinstance(value, str) or group_name_error(value) is not None:
        return ""
    return value.strip()


def empty_manifest() -> dict:
    return {"v": MANIFEST_VERSION, "bindings": {}, "notes": {}, "groups": {}, "suggestions": [], "description": "", "presets": {},
            "execution": {}, "execution_error": None, "execution_routing": None, "views": {}, "views_error": None}


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
    # A binding's section is derived from its target nodes' classes
    # (control_descriptors.section_for); the stored key is only a fallback for a
    # binding whose nodes are gone. Read tolerantly and never refused. Expiry:
    # drop the key from the schema at the first release that ships the Relay.
    section = raw.get("section", "workflow")
    if section not in SECTIONS:
        section = "workflow"
    advanced = raw.get("advanced", False)
    if not isinstance(advanced, bool):
        raise ManifestError("A Controls advanced flag must be true or false.")
    entry = {
        "label": _text(raw.get("label"), LABEL_MAX, "label").strip(),
        "section": section,
        "group": clean_group(raw.get("group")),
        "order": _order(raw.get("order")),
        "advanced": advanced,
        "note": _text(raw.get("note"), NOTE_MAX, "note"),
    }
    # Export-only facts. A saved manifest has none of them.
    status = raw.get("status")
    if status is not None:
        entry["status"] = status if status in EXPORT_STATUSES else "missing"
    entry["targets"] = _targets(raw.get("targets"))
    if _targets(raw.get("restore_targets")):
        entry["restorable"] = True
    mode = raw.get("seed_mode")
    entry["seed_mode"] = mode if mode in SEED_MODES else None
    # A canvas whose frontend has no row widgets for a row-group Control
    # (ComfyUI frontend before 1.57). Presentation only; the Sonder App edits
    # rows anyway, because its prompt is built on the server.
    if raw.get("rows_unavailable") is True:
        entry["rows_unavailable"] = True
    participation = raw.get("participation")
    if participation in PARTICIPATIONS:
        entry["participation"] = participation
        reason = raw.get("inactive_reason")
        if isinstance(reason, dict) and reason.get("mode") in INACTIVE_MODES and reason.get("by") in ("self", "parent"):
            label = reason.get("label")
            entry["inactive_reason"] = {"mode": reason["mode"], "by": reason["by"],
                                        "label": label[:LABEL_MAX] if isinstance(label, str) else ""}
    return entry


def _root_path(value: Any) -> str | None:
    return value if isinstance(value, str) and _ROOT_PATH_RE.match(value) and value not in FORBIDDEN_KEYS else None


def _execution_entry(raw: Any) -> dict:
    """One execution Control: presentation, its target and, exported, its state."""
    if not isinstance(raw, dict):
        raise ManifestError("An execution Control must be an object.")
    advanced = raw.get("advanced", False)
    if not isinstance(advanced, bool):
        raise ManifestError("A Controls advanced flag must be true or false.")
    target = raw.get("target")
    kind = target.get("kind") if isinstance(target, dict) else None
    if kind not in EXECUTION_KINDS:
        raise ManifestError("An execution Control names no node, subgraph or group.")
    stored = {"kind": kind}
    if kind == "group":
        group_id = target.get("group_id")
        if isinstance(group_id, bool) or not isinstance(group_id, int) or abs(group_id) > 2 ** 31:
            raise ManifestError("An execution Control names an invalid group.")
        stored["group_id"] = group_id
    entry = {
        "label": _text(raw.get("label"), LABEL_MAX, "label").strip(),
        "group": clean_group(raw.get("group")),
        "order": _order(raw.get("order")),
        "advanced": advanced,
        "note": _text(raw.get("note"), NOTE_MAX, "note"),
        "target": stored,
    }
    # Export-only facts, read tolerantly: the server rechecks them.
    status = raw.get("status")
    if status is not None:
        entry["status"] = status if status in EXECUTION_STATUSES else "missing"
    problem = raw.get("problem")
    if problem in EXECUTION_PROBLEMS:
        entry["problem"] = problem
    title = raw.get("title")
    if isinstance(title, str):
        entry["title"] = title[:LABEL_MAX]
    count = raw.get("member_count")
    if isinstance(count, int) and not isinstance(count, bool) and 0 <= count <= 100000:
        entry["member_count"] = count
    members = raw.get("members")
    if isinstance(members, list):
        clean = []
        for member in members[:MAX_EXECUTION_MEMBERS]:
            path = _root_path(member.get("path")) if isinstance(member, dict) else None
            if path is None:
                continue
            class_type, mode, label = member.get("class_type"), member.get("mode"), member.get("title")
            clean.append({"path": path, "class_type": class_type[:256] if isinstance(class_type, str) else "",
                          "mode": mode if mode in EXECUTION_MODES else "enabled",
                          "kind": "instance" if member.get("kind") == "instance" else "node",
                          "title": label[:LABEL_MAX] if isinstance(label, str) else ""})
        entry["members"] = clean
    return entry


def read_execution(raw: Any) -> tuple[dict, str | None]:
    """The execution section, or why it is off. Never raises and never hides
    the workflow's other Controls. Tolerant like ``read_presets``: an invalid
    entry is skipped (and preserved, since reads never rewrite) and at most
    ``MAX_EXECUTION`` valid entries are read. Only another section version
    turns the section off, because its entries may mean something else.
    web/js/control_bindings.js ``readExecution`` mirrors it."""
    if not isinstance(raw, dict):
        return {}, None
    version = raw.get("v", EXECUTION_VERSION)
    # JSON 1.0 is 1 in JavaScript; True is not.
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version != EXECUTION_VERSION:
        return {}, "This workflow's node switches come from another Sonder version, so they are off."
    controls = raw.get("controls")
    if not isinstance(controls, dict):
        return {}, None
    result = {}
    for identity, entry in controls.items():
        if len(result) >= MAX_EXECUTION:
            break
        try:
            result[_check_id(identity, "execution")] = _execution_entry(entry)
        except ManifestError:
            continue
    return result, None


def _view_entry(raw: Any) -> dict:
    """One View: its presentation and, exported, its source's facts."""
    if not isinstance(raw, dict):
        raise ManifestError("A View must be an object.")
    order = raw.get("order")
    # JavaScript reads 5.0 as 5 (readViews): a whole float is that whole number here too.
    if isinstance(order, float) and order.is_integer():
        order = int(order)
    entry = {"label": _text(raw.get("label"), LABEL_MAX, "label").strip(), "order": _order(order)}
    # Export-only facts, read tolerantly: the server rechecks them.
    status = raw.get("status")
    if status is not None:
        entry["status"] = status if status in VIEW_STATUSES else "missing"
    path = clean_path(raw.get("path"))
    if path is not None:
        entry["path"] = path
    for key, limit in (("class_type", 256), ("title", LABEL_MAX)):
        value = raw.get(key)
        if isinstance(value, str):
            entry[key] = value[:limit]
    participation = raw.get("participation")
    if participation in ("active", "inactive"):
        entry["participation"] = participation
        reason = raw.get("inactive_reason")
        if isinstance(reason, dict) and reason.get("mode") in INACTIVE_MODES and reason.get("by") in ("self", "parent"):
            label = reason.get("label")
            entry["inactive_reason"] = {"mode": reason["mode"], "by": reason["by"],
                                        "label": label[:LABEL_MAX] if isinstance(label, str) else ""}
    return entry


def _js_key_order(items: dict) -> list:
    """``items``' keys in JavaScript's object order (``Object.entries``): array
    index keys ("0", "12", no leading zero, below 2**32 - 1) ascending, then
    the rest as written. A reader capped at ``MAX_VIEWS`` keeps the same Views
    as readViews this way."""
    def index(key: Any) -> int | None:
        if isinstance(key, str) and key.isascii() and key.isdecimal() and (key == "0" or not key.startswith("0")):
            value = int(key)
            return value if value < 2 ** 32 - 1 else None
        return None
    keys = list(items)
    indexed = sorted((k for k in keys if index(k) is not None), key=index)
    return indexed + [k for k in keys if index(k) is None]


def read_views(raw: Any) -> tuple[dict, str | None]:
    """The Views section, or why it is off. Never raises and never hides the
    workflow's Controls; tolerant like ``read_execution`` (an invalid entry is
    skipped and preserved, at most ``MAX_VIEWS`` are read, and only another
    section version turns it off). web/js/control_bindings.js ``readViews``
    mirrors it."""
    if not isinstance(raw, dict):
        return {}, None
    version = raw.get("v", VIEWS_VERSION)
    if isinstance(version, bool) or not isinstance(version, (int, float)) or version != VIEWS_VERSION:
        return {}, "This workflow's Views come from another Sonder version, so they are off."
    items = raw.get("items")
    if not isinstance(items, dict):
        return {}, None
    result = {}
    for identity in _js_key_order(items):
        if len(result) >= MAX_VIEWS:
            break
        if identity in FORBIDDEN_KEYS:
            continue
        try:
            result[_check_id(identity, "View")] = _view_entry(items[identity])
        except ManifestError:
            continue
    return result, None


def _execution_member(member: Any) -> dict | None:
    """A preset's execution member: a node's mode and identity, or a group's
    exact member modes. None when malformed."""
    if not isinstance(member, dict):
        return None
    if "modes" in member:
        modes = member.get("modes")
        if (not isinstance(modes, dict) or not 1 <= len(modes) <= MAX_EXECUTION_MEMBERS
                or any(_root_path(path) is None or mode not in EXECUTION_MODES for path, mode in modes.items())):
            return None
        return {"modes": dict(modes)}
    node = member.get("node")
    if member.get("mode") not in EXECUTION_MODES or not isinstance(node, dict):
        return None
    path, class_type = _root_path(node.get("path")), node.get("class_type")
    if path is None or not isinstance(class_type, str) or not class_type or len(class_type) > 256:
        return None
    return {"mode": member["mode"], "node": {"path": path, "class_type": class_type}}


def execution_effects(members: dict) -> dict | None:
    """``{path: mode}`` of a preset's execution members; None on a conflict."""
    effects: dict[str, str] = {}
    for member in members.values():
        pairs = member["modes"].items() if "modes" in member else [(member["node"]["path"], member["mode"])]
        for path, mode in pairs:
            if effects.setdefault(path, mode) != mode:
                return None
    return effects


def read_presets(raw: Any, *, limit: int | None = MAX_PRESETS) -> dict:
    """Tolerant, non-mutating projection. Hygiene is checked by the manifest.

    Invalid members are ignored individually; an empty/over-cap preset is
    ignored. Only valid entries count toward the preset cap.
    """
    result = {}
    if not isinstance(raw, dict):
        return result
    for identity, entry in raw.items():
        if (not isinstance(identity, str) or not ID_RE.fullmatch(identity)
                or identity in FORBIDDEN_KEYS or not isinstance(entry, dict)):
            continue
        name, description, order = entry.get("name"), entry.get("description", ""), entry.get("order", 0)
        if (not isinstance(name, str) or not name.strip() or len(name) > LABEL_MAX
                or not isinstance(description, str) or len(description) > DESCRIPTION_MAX
                or isinstance(order, bool) or not isinstance(order, int) or abs(order) > ORDER_LIMIT):
            continue
        members = {}
        for bid, member in (entry.get("members", {}).items() if isinstance(entry.get("members"), dict) else []):
            if (not isinstance(bid, str) or not ID_RE.fullmatch(bid) or bid in FORBIDDEN_KEYS
                    or not isinstance(member, dict) or "value" not in member):
                continue
            value, mode = member["value"], member.get("seed_mode")
            # A row-group Control's member is its row list (control_rows).
            if not (value is None or isinstance(value, (str, bool, int, float)) or control_rows.value_shape_ok(value)):
                continue
            if isinstance(value, float) and not math.isfinite(value):
                continue
            if "seed_mode" in member and mode not in SEED_MODES:
                continue
            members[bid] = {"value": value, **({"seed_mode": mode} if "seed_mode" in member else {})}
        execution = {}
        for eid, member in (entry.get("execution", {}).items() if isinstance(entry.get("execution"), dict) else []):
            if not isinstance(eid, str) or not ID_RE.fullmatch(eid) or eid in FORBIDDEN_KEYS:
                continue
            clean = _execution_member(member)
            if clean is not None:
                execution[eid] = clean
        effects = execution_effects(execution)
        # Distinct node effects count beside value and seed-mode targets; a
        # preset that sets one node two ways is unreadable, never half-applied.
        if (not (members or execution) or effects is None
                or sum(1 + ("seed_mode" in m) for m in members.values()) + len(effects) > MAX_PRESET_TARGETS):
            continue
        if limit is not None and len(result) >= limit:
            break
        result[identity] = {"name": name, "description": description, "order": order, "members": members,
                            **({"execution": execution} if execution else {})}
    return result


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
    if not isinstance(groups_raw, dict):
        groups_raw = {}  # cosmetic: read as no explicit group order
    if not isinstance(suggestions_raw, list) or len(suggestions_raw) > MAX_SUGGESTIONS:
        raise ManifestError("The Controls manifest has too many unlisted anchors.")

    manifest = empty_manifest()
    manifest["presets"] = read_presets(raw.get("presets"))
    manifest["execution"], manifest["execution_error"] = read_execution(raw.get("execution"))
    manifest["views"], manifest["views_error"] = read_views(raw.get("views"))
    # The export-only routing capsule (control_routing reads it), only beside a
    # section this version reads.
    section = raw.get("execution")
    if isinstance(section, dict) and not manifest["execution_error"]:
        manifest["execution_routing"] = section.get("routing")
    manifest["description"] = _text(raw.get("description"), DESCRIPTION_MAX, "description")
    for binding_id, entry in bindings_raw.items():
        manifest["bindings"][_check_id(binding_id)] = _binding(entry)
    for note_id, entry in notes_raw.items():
        if not isinstance(entry, dict):
            raise ManifestError("A Controls note must be an object.")
        manifest["notes"][_check_id(note_id, "note")] = {
            "text": _text(entry.get("text"), NOTE_MAX, "note"),
            "group": clean_group(entry.get("group")),
            "order": _order(entry.get("order")),
        }
    # Tolerant: an invalid entry is dropped, names that collide once stripped
    # merge (the first entry's order wins) and at most MAX_GROUPS are read.
    # Writers refuse what this drops (control_descriptors.validate_binding_changes).
    for name, entry in groups_raw.items():
        clean = clean_group(name)
        order = entry.get("order", 0) if isinstance(entry, dict) else None
        if (not clean or clean in manifest["groups"] or isinstance(order, bool) or not isinstance(order, int)
                or not -ORDER_LIMIT <= order <= ORDER_LIMIT):
            continue
        if len(manifest["groups"]) >= MAX_GROUPS:
            break
        manifest["groups"][clean] = {"order": order}
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
