"""Output Views: named inspection sources for the viewport's panes.

A View names one output node (an ``OUTPUT_NODE`` class: Preview Image, Save
Video, Sonder Preview Video...) whose newest result a pane shows. The manifest
(``control_manifest.read_views``) owns the View's id, label and order; the
canvas locates its node with an anchor (``properties.sonderViews``) and exports
the node's path, class and title. Nothing here trusts that export blindly: a
View is ``ok`` only when its node is in the prompt with the exported class
(or, on the canvas, in the inactive bodies when it is muted or bypassed).

A View is inspection only: it never enters a widget writer, a seed loop, a
preset, Controls-at-run provenance or project data.

Pure and standard-library only, like the other Controls modules.
"""

from __future__ import annotations

import copy
from typing import Any, Callable

from . import control_manifest as cm

SOURCE = "view"
MAX_CANDIDATES = 128
# Never a View: they have no media of their own.
EXCLUDED_CLASSES = frozenset({"SonderEditor", cm.RELAY_CLASS_TYPE})

REASONS = {
    "missing": "This output is no longer in the workflow. Remove the View, or add the output again.",
    "duplicate": "Two nodes carry this View. Remove one of them.",
    "kind": "This node is not an output node any more. Remove the View.",
    "unsupported": "This node can't be a View.",
    "stale": "This View changed since Controls last read it. Refresh the Controls panel.",
}
INACTIVE_REASONS = {"muted": "Its node is muted.", "bypassed": "Its node is bypassed."}
OFF_AT_EXPORT_REASON = "Its node was off when this workflow was exported."
SDK_RESET_REASON = "Only the Sonder App keeps its own copy of a View to reset."


def _class_lookup_default(class_type: str):
    # The descriptors' lookup (ComfyUI's NODE_CLASS_MAPPINGS): one authority.
    from . import control_descriptors
    return control_descriptors._comfy_class_lookup(class_type)


def is_output_class(class_type: Any, class_lookup: Callable[[str], Any] | None = None) -> bool:
    """An ``OUTPUT_NODE`` class (V1 attribute or the V3 schema's classproperty)."""
    if not isinstance(class_type, str) or not class_type or class_type in EXCLUDED_CLASSES:
        return False
    try:
        node_class = (class_lookup or _class_lookup_default)(class_type)
    except Exception:
        return False
    try:
        return node_class is not None and getattr(node_class, "OUTPUT_NODE", False) is True
    except Exception:
        return False


def anchor_of(path: str) -> tuple[str, str]:
    """``(host, rel)``: the root node carrying the anchor, and the path inside it."""
    host, _, rel = path.partition(":")
    return host, rel


def _title(node: dict) -> str:
    meta = node.get("_meta") if isinstance(node.get("_meta"), dict) else {}
    title = meta.get("title")
    return (title if isinstance(title, str) and title else str(node.get("class_type") or ""))[:cm.LABEL_MAX]


def _off_by_modes(path: str, off: dict | None) -> dict | None:
    """The Sonder App's own switch state (``node_modes``) as an inactive reason."""
    if not off:
        return None
    root = path.split(":", 1)[0]
    if path in off:
        return {"mode": off[path], "by": "self", "label": ""}
    if root in off:
        return {"mode": off[root], "by": "parent", "label": ""}
    return None


def output_candidates(prompt: dict, authoring: dict | None = None, *,
                      class_lookup: Callable[[str], Any] | None = None,
                      off: dict | None = None) -> tuple[list[dict], bool]:
    """Every output node a View could name, in prompt order, and whether the
    list was cut at ``MAX_CANDIDATES``. Muted and bypassed outputs come from
    the canvas's inactive bodies (``authoring``) and carry ``inactive``; the
    Sonder App marks the ones its own switches turned off (``off``). Always
    computed, whatever the panel shows."""
    found: list[dict] = []
    truncated = False

    def push(path: str, node: dict, inactive: dict | None) -> None:
        nonlocal truncated
        if cm.clean_path(path) is None or not is_output_class(node.get("class_type"), class_lookup):
            return
        if len(found) >= MAX_CANDIDATES:
            truncated = True
            return
        host, rel = anchor_of(path)
        row = {"path": path, "class_type": str(node["class_type"])[:256], "title": _title(node), "host": host, "rel": rel}
        if inactive:
            row["inactive"] = inactive
        found.append(row)

    for path, node in (prompt or {}).items():
        if isinstance(node, dict):
            push(str(path), node, _off_by_modes(str(path), off))
    for path, body in (authoring or {}).items():
        if isinstance(body, dict) and path not in (prompt or {}):
            push(str(path), body, {"mode": body.get("mode", "muted"), "by": body.get("by", "self"),
                                   "label": str(body.get("label") or "")[:cm.LABEL_MAX]})
    return found, truncated


def default_label(entry: dict) -> str:
    return (entry.get("title") or entry.get("class_type") or "Output")[:cm.LABEL_MAX]


def _inactive_text(reason: dict | None) -> str:
    if not reason:
        return ""
    text = INACTIVE_REASONS.get(reason.get("mode"), INACTIVE_REASONS["muted"])
    if reason.get("by") == "parent":
        what = "muted" if reason.get("mode") == "muted" else "bypassed"
        label = reason.get("label") or "its subgraph"
        text = f"It is inside {label}, which is {what}."
    return text


def describe_one(prompt: dict, identity: str, entry: dict, *, authoring: dict | None = None,
                 class_lookup: Callable[[str], Any] | None = None, off: dict | None = None,
                 origin: str = "authored") -> dict:
    path, class_type = entry.get("path"), entry.get("class_type") or ""
    descriptor: dict[str, Any] = {
        "source": SOURCE, "view_id": identity, "origin": origin,
        "label": entry.get("label") or default_label(entry), "default_label": default_label(entry),
        "order": entry.get("order", 0), "path": path, "class_type": class_type,
        "title": entry.get("title") or class_type, "participation": None, "reason": None, "runnable": False,
    }
    status = entry.get("status") or "missing"
    reason_code = None
    if status == "ok":
        node = prompt.get(path) if path else None
        body = (authoring or {}).get(path) if path else None
        if not path or not class_type:
            status, reason_code = "missing", "stale"
        elif not is_output_class(class_type, class_lookup):
            status = "kind"
        elif isinstance(node, dict) and node.get("class_type") == class_type:
            inactive = _off_by_modes(path, off)
            if inactive:
                descriptor["participation"], descriptor["inactive_reason"] = "inactive", inactive
            else:
                descriptor["participation"] = "active"
        elif isinstance(body, dict) and body.get("class_type") == class_type:
            descriptor["participation"] = "inactive"
            descriptor["inactive_reason"] = {"mode": body.get("mode", "muted"), "by": body.get("by", "self"),
                                             "label": str(body.get("label") or "")[:cm.LABEL_MAX]}
        elif authoring is None and entry.get("participation") == "inactive" and not isinstance(node, dict):
            # An import: its node was off at export, so it is not in this prompt.
            descriptor["participation"] = "inactive"
            descriptor["inactive_reason"] = entry.get("inactive_reason") or {"mode": "muted", "by": "self", "label": ""}
            descriptor["off_at_export"] = True
        else:
            status, reason_code = "missing", "stale"
    descriptor["status"] = status
    if status == "ok":
        descriptor["runnable"] = descriptor["participation"] == "active"
        if descriptor["participation"] == "inactive":
            descriptor["reason"] = (OFF_AT_EXPORT_REASON if descriptor.get("off_at_export")
                                    else _inactive_text(descriptor.get("inactive_reason")))
        return descriptor
    descriptor["reason"] = REASONS.get(reason_code) or REASONS.get(status) or REASONS["unsupported"]
    return descriptor


def _sort_key(descriptor: dict) -> tuple:
    return (descriptor["order"], descriptor["label"].lower(), descriptor["view_id"])


def describe_views(prompt: dict, manifest: dict, *, authoring: dict | None = None,
                   class_lookup: Callable[[str], Any] | None = None) -> tuple[list[dict], str | None]:
    """Descriptors for the manifest's Views, and a section error."""
    rows = [describe_one(prompt, identity, entry, authoring=authoring, class_lookup=class_lookup)
            for identity, entry in (manifest.get("views") or {}).items()]
    rows.sort(key=_sort_key)
    return rows, manifest.get("views_error")


def add_to_view(view: dict, prompt: dict, manifest: dict, *, authoring: dict | None = None,
                class_lookup: Callable[[str], Any] | None = None) -> None:
    """``views``, ``views_error`` and ``view_candidates`` in a Controls view.
    The full candidate list stays server-side (``_view_inventory``) for
    validation; the view carries at most ``MAX_CANDIDATES``."""
    view["views"], error = describe_views(prompt, manifest, authoring=authoring, class_lookup=class_lookup)
    if error:
        view["views_error"] = error
    # The exported entries, for the Sonder App's overlay (apply_sdk_views).
    view["_view_entries"] = copy.deepcopy(manifest.get("views") or {})
    inventory, truncated = output_candidates(prompt, authoring, class_lookup=class_lookup)
    view["_view_inventory"] = inventory
    _publish_candidates(view, truncated)


def _publish_candidates(view: dict, truncated: bool = False) -> None:
    taken = {d.get("path") for d in view.get("views", []) if d.get("path")}
    listed = [dict(c) for c in view.get("_view_inventory", []) if c["path"] not in taken]
    view["view_candidates"] = listed[:MAX_CANDIDATES]
    view["view_candidates_truncated"] = truncated or len(listed) > MAX_CANDIDATES


# ── The Sonder App ───────────────────────────────────────────────────────────

def stored_views(stored: dict) -> dict:
    """The sidecar's ``views``: ``{id: {label, order, source: {path, class_type}}
    | {removed: true}}``, copy-on-write over the workflow's Views. Anything
    malformed is preserved on disk and ignored here."""
    raw = stored.get("views") if isinstance(stored, dict) else None
    found = {}
    for identity, entry in (raw.items() if isinstance(raw, dict) else []):
        if not isinstance(identity, str) or not cm.ID_RE.match(identity) or identity in cm.FORBIDDEN_KEYS:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("removed") is True:
            found[identity] = {"removed": True}
            continue
        source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
        path, class_type = cm.clean_path(source.get("path")), source.get("class_type")
        label, order = entry.get("label", ""), entry.get("order", 0)
        if (path is None or not isinstance(class_type, str) or not class_type or len(class_type) > 256
                or not isinstance(label, str) or len(label) > cm.LABEL_MAX
                or isinstance(order, bool) or not isinstance(order, int) or abs(order) > cm.ORDER_LIMIT):
            continue
        found[identity] = {"label": label.strip(), "order": order, "source": {"path": path, "class_type": class_type}}
    return found


def apply_sdk_views(view: dict, prompt: dict, stored: dict, *, class_lookup: Callable[[str], Any] | None = None) -> None:
    """The app's Views: the workflow's own, overlaid by the sidecar (a copy
    replaces, ``removed`` hides, an app-only entry adds), resolved by
    ``(path, class_type)`` against the template, with the app's own switches
    marking a View off. Called after ``control_execution.apply_sdk_state``."""
    if view.get("views_error") or (view.get("relay") or {}).get("status") == "error":
        return
    off = view.get("_node_modes") or {}
    authored = view.get("_view_entries") or {}
    overlay = stored_views(stored)
    rows = []
    for identity, raw in authored.items():
        entry = overlay.get(identity)
        if entry is None:
            rows.append(describe_one(prompt, identity, raw, class_lookup=class_lookup, off=off))
        elif not entry.get("removed"):
            rows.append(_describe_stored(prompt, identity, entry, "app_copy", class_lookup, off, authored=raw))
    for identity, entry in overlay.items():
        if identity in authored or entry.get("removed"):
            continue
        rows.append(_describe_stored(prompt, identity, entry, "app", class_lookup, off))
    rows.sort(key=_sort_key)
    # As the reader: at most MAX_VIEWS (the sidecar's projection check refuses more).
    view["views"] = rows[:cm.MAX_VIEWS]
    inventory, truncated = output_candidates(prompt, None, class_lookup=class_lookup, off=off)
    view["_view_inventory"] = inventory
    _publish_candidates(view, truncated)


def _describe_stored(prompt: dict, identity: str, entry: dict, origin: str, class_lookup, off,
                     authored: dict | None = None) -> dict:
    path, class_type = entry["source"]["path"], entry["source"]["class_type"]
    node = prompt.get(path)
    present = isinstance(node, dict) and node.get("class_type") == class_type
    resolved = {"label": entry["label"], "order": entry["order"], "path": path, "class_type": class_type,
                "title": _title(node) if present else class_type, "status": "ok" if present else "missing"}
    # A copy of a View whose node was off at export: that node is not in this
    # prompt, and renaming or moving the View must not turn it missing.
    if (not present and not isinstance(node, dict) and isinstance(authored, dict)
            and authored.get("participation") == "inactive" and authored.get("path") == path
            and authored.get("class_type") == class_type):
        resolved.update({"status": "ok", "participation": "inactive", "inactive_reason": authored.get("inactive_reason"),
                         "title": authored.get("title") or class_type})
    return describe_one(prompt, identity, resolved, class_lookup=class_lookup, off=off, origin=origin)


def curated_views(view: dict) -> dict | None:
    """The export's ``views`` section as the app presents it (``export_curated``):
    every View it shows, by its source, labels and order materialized."""
    rows = [d for d in view.get("views", []) if d.get("status") == "ok" and d.get("path")]
    if not rows and not view.get("views"):
        return None
    return {"v": cm.VIEWS_VERSION, "items": {
        d["view_id"]: {"label": d["label"] if d["label"] != d["default_label"] else "", "order": d["order"],
                       "status": "ok", "path": d["path"], "class_type": d["class_type"], "title": d.get("title") or "",
                       **({"participation": "inactive", "inactive_reason": d["inactive_reason"]}
                          if d.get("participation") == "inactive" and d.get("inactive_reason") else {})}
        for d in rows}}


# ── Validation ───────────────────────────────────────────────────────────────

PRESENTATION_FIELDS = ("label", "order")


def table_entries(view: dict) -> dict:
    """``view:<id>:<field>`` presentation targets the view accepts."""
    table = {}
    for descriptor in view.get("views", []) or []:
        identity = descriptor.get("view_id")
        if isinstance(identity, str) and identity:
            for field in PRESENTATION_FIELDS:
                table[f"view:{identity}:{field}"] = (descriptor, field)
            table[f"view:{identity}:reset"] = (descriptor, "reset")
    return table


def coerce_presentation(descriptor: dict, field: str, raw: Any, mode: str | None) -> Any:
    from .control_descriptors import ControlValueError, PANEL_LABEL_MAX_CHARS, coerce_panel_layout

    if field == "label":
        if not isinstance(raw, str) or len(raw.strip()) > PANEL_LABEL_MAX_CHARS:
            raise ControlValueError("A label must be text of at most 120 characters.")
        return raw.strip()
    if field == "order":
        return coerce_panel_layout("order", raw)
    if field == "reset":
        if mode != "sdk" or descriptor.get("origin") != "app_copy":
            raise ControlValueError(SDK_RESET_REASON)
        if raw is not True:
            raise ControlValueError("Send true to reset.")
        return True
    raise ControlValueError("This target is unavailable. Refresh the Controls panel.")


def validate_structural(view: dict, identity: str, op: str, raw: Any, mode: str | None, state: dict, next_order) -> dict:
    """``view:<id>:create {candidate: {path}, label?}`` and ``view:<id>:remove
    true``. ``state`` carries this batch's claims. The mounted pre-check
    (``relay``) checks shape only: the canvas writer revalidates."""
    from .control_descriptors import ControlValueError, PANEL_LABEL_MAX_CHARS

    if mode not in ("canvas", "relay", "sdk"):
        raise ControlValueError("This target is unavailable. Refresh the Controls panel.")
    if view.get("views_error"):
        raise ControlValueError(view["views_error"])
    existing = {d["view_id"]: d for d in view.get("views", []) or []}.get(identity)
    shape_only = mode == "relay"
    if op == "remove":
        if raw is not True:
            raise ControlValueError("Send true to remove.")
        return {"remove": True} if existing is not None or shape_only else {"noop": True}
    if op != "create":
        raise ControlValueError("This target is unavailable. Refresh the Controls panel.")
    if not isinstance(raw, dict) or not isinstance(raw.get("candidate"), dict):
        raise ControlValueError("Choose an output.")
    path = cm.clean_path(raw["candidate"].get("path"))
    if path is None:
        raise ControlValueError("Choose an output.")
    label = raw.get("label")
    if label is not None and (not isinstance(label, str) or len(label.strip()) > PANEL_LABEL_MAX_CHARS):
        raise ControlValueError("A label must be text of at most 120 characters.")
    if path in state.setdefault("claimed", set()):
        raise ControlValueError("Two changes add the same output.")
    state["claimed"].add(path)
    if shape_only:
        return {"candidate": {"path": path}, "label": (label or "").strip()}
    if existing is not None:
        if existing.get("path") == path:
            return {"noop": True}  # a retry of a create that landed
        raise ControlValueError("This View id is already in use.")
    found = next((c for c in view.get("_view_inventory", []) if c["path"] == path), None)
    if found is None:
        raise ControlValueError("This output is no longer in the workflow. Refresh the Controls panel.")
    if any(d.get("path") == path for d in view.get("views", []) or []):
        raise ControlValueError("This output already has a View.")
    count = state.setdefault("count", len(view.get("views", []) or []))
    if count >= cm.MAX_VIEWS:
        raise ControlValueError(f"At most {cm.MAX_VIEWS} Views fit in one workflow.")
    state["count"] = count + 1
    return {"candidate": {key: found[key] for key in ("path", "class_type", "host", "rel")},
            "label": (label or "").strip(), "order": next_order()}


def view_ids(view: dict) -> set:
    return {d.get("view_id") for d in view.get("views", []) or [] if d.get("view_id")}


def store_sdk_change(data: dict, view: dict, identity: str, field: str, value: Any) -> None:
    """One validated ``view:*`` change in the sidecar (inside its one mutation)."""
    views = data.setdefault("views", {})
    if not isinstance(views, dict):
        views = data["views"] = {}
    current = {d["view_id"]: d for d in view.get("views", []) or []}.get(identity)
    if field == "create":
        candidate = value["candidate"]
        views[identity] = {"label": value.get("label", ""), "order": value.get("order", 0),
                           "source": {"path": candidate["path"], "class_type": candidate["class_type"]}}
        return
    if field == "remove":
        if current is not None and current.get("origin") in ("authored", "app_copy"):
            views[identity] = {"removed": True}
        else:
            views.pop(identity, None)
        return
    if field == "reset":
        views.pop(identity, None)
        return
    # label / order: a full copy, so the entry stands alone when the workflow changes.
    entry = views.get(identity)
    if not isinstance(entry, dict) or entry.get("removed"):
        if current is None:
            return
        entry = views[identity] = {"label": "" if current["label"] == current["default_label"] else current["label"],
                                   "order": current["order"],
                                   "source": {"path": current["path"], "class_type": current["class_type"]}}
    entry[field] = value
