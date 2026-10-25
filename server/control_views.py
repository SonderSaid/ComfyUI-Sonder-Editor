"""Output Views: named inspection sources for the viewport's panes.

A View names one output node (an ``OUTPUT_NODE`` class: Preview Image, Save
Video, Sonder Preview Video...) whose newest result a pane shows, or, with
``feed: "sampling"`` (Phase 4), one node whose live sampling previews a pane
shows: a sampler, KJNodes Model Preview Override, or any node a recent run saw
publish previews, but never an output node, which keeps its output View. One
View per node, whatever its feed. The manifest
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
from . import view_timing

SOURCE = "view"
MAX_CANDIDATES = 128
FEED_OUTPUT, FEED_SAMPLING = cm.VIEW_FEED_OUTPUT, cm.VIEW_FEED_SAMPLING
# Always offered as sampling-preview sources (any other node is offered once a
# recent run saw it publish). web/js/control_bindings.js mirrors it.
SAMPLING_CLASSES = frozenset({"KSampler", "KSamplerAdvanced", "SamplerCustom", "SamplerCustomAdvanced",
                              "ModelPreviewOverrideKJ"})

# Never a View: they have no media of their own.
EXCLUDED_CLASSES = frozenset({"SonderEditor", cm.RELAY_CLASS_TYPE})

REASONS = {
    "missing": "This output is no longer in the workflow. Remove the View, or add the output again.",
    "duplicate": "Two nodes carry this View. Remove one of them.",
    "kind": "This node is not an output node any more. Remove the View.",
    "unsupported": "This node can't be a View.",
    "sampling_kind": "This node can't show sampling previews here. Remove the View.",
    "sampling_output": "This node is an output node: give it an output View instead. Remove this one.",
    "sampling_missing": "This node is no longer in the workflow. Remove the View, or add the node again.",
    "stale": "This View changed since Controls last read it. Refresh the Controls panel.",
}
INACTIVE_REASONS = {"muted": "Its node is muted.", "bypassed": "Its node is bypassed."}
OFF_AT_EXPORT_REASON = "Its node was off when this workflow was exported."
SDK_RESET_REASON = "Only the Sonder App keeps its own copy of a View to reset."
SAMPLING_RUN_REASON = "A sampling preview shows while another output runs."
SAMPLING_TIMING_REASON = "A sampling preview plays at its own rate; it can't follow the timeline."


def feed_of(entry: Any) -> str:
    return FEED_SAMPLING if isinstance(entry, dict) and entry.get("feed") == FEED_SAMPLING else FEED_OUTPUT


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
    return _node_rows(prompt, authoring, off, lambda class_type: is_output_class(class_type, class_lookup),
                      MAX_CANDIDATES)


def _node_rows(prompt: dict, authoring: dict | None, off: dict | None, keep: Callable[[str], bool],
               limit: int) -> tuple[list[dict], bool]:
    """``{path, class_type, title, host, rel, inactive?}`` for the prompt's nodes
    (muted and bypassed ones from ``authoring``) whose class ``keep`` accepts."""
    found: list[dict] = []
    truncated = False

    def push(path: str, node: dict, inactive: dict | None) -> None:
        nonlocal truncated
        class_type = node.get("class_type")
        if (cm.clean_path(path) is None or not isinstance(class_type, str) or not class_type
                or class_type in EXCLUDED_CLASSES or not keep(class_type)):
            return
        if len(found) >= limit:
            truncated = True
            return
        host, rel = anchor_of(path)
        row = {"path": path, "class_type": class_type[:256], "title": _title(node), "host": host, "rel": rel}
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


def sampling_candidates(prompt: dict, authoring: dict | None = None, *, off: dict | None = None,
                        class_lookup: Callable[[str], Any] | None = None) -> tuple[list[dict], bool]:
    """The core sampling-preview sources in the prompt (``SAMPLING_CLASSES``, never an output node)."""
    return _node_rows(prompt, authoring, off, lambda class_type: class_type in SAMPLING_CLASSES
                      and not is_output_class(class_type, class_lookup), MAX_CANDIDATES)


def sampling_index(prompt: dict, authoring: dict | None = None, *,
                   class_lookup: Callable[[str], Any] | None = None) -> dict[str, str]:
    """``{path: class_type}`` of every node a sampling View could name: the core
    classes, and any other node a recent run saw publish previews (the pane picker
    offers those). Never an output node, the Editor or the Relay. Uncapped, and
    only a path and a class per node: validation only, never published."""
    index: dict[str, str] = {}
    for source in (prompt or {}), (authoring or {}):
        for path, node in source.items():
            class_type = node.get("class_type") if isinstance(node, dict) else None
            if (str(path) in index or cm.clean_path(str(path)) is None or not isinstance(class_type, str)
                    or not class_type or class_type in EXCLUDED_CLASSES or is_output_class(class_type, class_lookup)):
                continue
            index[str(path)] = class_type[:256]
    return index


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
    feed = feed_of(entry)
    descriptor: dict[str, Any] = {
        "source": SOURCE, "view_id": identity, "origin": origin, "feed": feed,
        "label": entry.get("label") or default_label(entry), "default_label": default_label(entry),
        "order": entry.get("order", 0), "path": path, "class_type": class_type,
        "title": entry.get("title") or class_type, "participation": None, "reason": None, "runnable": False,
        # How the output's frames sit on the scene (Phase 3B), or None: unlinked.
        # A sampling preview plays at its own rate: a stored timing is ignored.
        "timing": (copy.deepcopy(entry.get("timing")) if isinstance(entry.get("timing"), dict)
                   and feed == FEED_OUTPUT else None),
    }
    status = entry.get("status") or "missing"
    reason_code = None
    if status == "ok":
        node = prompt.get(path) if path else None
        body = (authoring or {}).get(path) if path else None
        if not path or not class_type:
            status, reason_code = "missing", "stale"
        elif feed == FEED_SAMPLING and class_type in EXCLUDED_CLASSES:
            status = "unsupported"
        elif feed == FEED_SAMPLING and is_output_class(class_type, class_lookup):
            status, reason_code = "kind", "sampling_output"
        elif feed == FEED_OUTPUT and not is_output_class(class_type, class_lookup):
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
        descriptor["runnable"] = feed == FEED_OUTPUT and descriptor["participation"] == "active"
        if feed == FEED_SAMPLING:
            descriptor["run_reason"] = SAMPLING_RUN_REASON
        if descriptor["participation"] == "inactive":
            descriptor["reason"] = (OFF_AT_EXPORT_REASON if descriptor.get("off_at_export")
                                    else _inactive_text(descriptor.get("inactive_reason")))
        return descriptor
    if feed == FEED_SAMPLING and status == "kind" and reason_code is None:
        reason_code = "sampling_kind"
    if feed == FEED_SAMPLING and status == "missing" and reason_code is None:
        reason_code = "sampling_missing"
    descriptor["reason"] = REASONS.get(reason_code) or REASONS.get(status) or REASONS["unsupported"]
    return descriptor


def _sort_key(descriptor: dict) -> tuple:
    return (descriptor["order"], descriptor["label"].lower(), descriptor["view_id"])


_ORIGIN_RANK = {"authored": 0, "app_copy": 1, "app": 2}


def _mark_shared_paths(rows: list[dict]) -> None:
    """One View per node: a later View on a node another already shows reads
    ``duplicate`` (the workflow's own first). The canvas anchor allows only one;
    an import or a Sonder App sidecar over a replaced workflow can hold two."""
    taken: set = set()
    for row in sorted(rows, key=lambda d: (_ORIGIN_RANK.get(d.get("origin"), 3), _sort_key(d))):
        if row.get("status") != "ok" or not row.get("path"):
            continue
        if row["path"] in taken:
            row.update(status="duplicate", participation=None, runnable=False, reason=REASONS["duplicate"])
            row.pop("inactive_reason", None)
            row.pop("run_reason", None)
        else:
            taken.add(row["path"])


def describe_views(prompt: dict, manifest: dict, *, authoring: dict | None = None,
                   class_lookup: Callable[[str], Any] | None = None) -> tuple[list[dict], str | None]:
    """Descriptors for the manifest's Views, and a section error."""
    rows = [describe_one(prompt, identity, entry, authoring=authoring, class_lookup=class_lookup)
            for identity, entry in (manifest.get("views") or {}).items()]
    _mark_shared_paths(rows)
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
    sampling, sampling_truncated = sampling_candidates(prompt, authoring, class_lookup=class_lookup)
    view["_view_sampling_index"] = sampling_index(prompt, authoring, class_lookup=class_lookup)
    _publish_candidates(view, truncated, sampling, sampling_truncated)


def _publish_candidates(view: dict, truncated: bool = False, sampling: list | None = None,
                        sampling_truncated: bool = False) -> None:
    """The pane picker's lists, minus every path that already has a View (one per node)."""
    taken = {d.get("path") for d in view.get("views", []) if d.get("path")}
    listed = [dict(c) for c in view.get("_view_inventory", []) if c["path"] not in taken]
    view["view_candidates"] = listed[:MAX_CANDIDATES]
    view["view_candidates_truncated"] = truncated or len(listed) > MAX_CANDIDATES
    offered = [dict(c) for c in (sampling or []) if c["path"] not in taken]
    view["view_sampling_candidates"] = offered[:MAX_CANDIDATES]
    view["view_sampling_candidates_truncated"] = sampling_truncated or len(offered) > MAX_CANDIDATES


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
        feed = entry.get("feed")
        if feed not in (None, FEED_OUTPUT, FEED_SAMPLING):
            continue
        found[identity] = {"label": label.strip(), "order": order, "source": {"path": path, "class_type": class_type}}
        if feed == FEED_SAMPLING:
            found[identity]["feed"] = FEED_SAMPLING
        timing = view_timing.clean_timing(entry.get("timing"))
        if timing is not None:
            found[identity]["timing"] = timing
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
    _mark_shared_paths(rows)
    rows.sort(key=_sort_key)
    # As the reader: at most MAX_VIEWS (the sidecar's projection check refuses more).
    view["views"] = rows[:cm.MAX_VIEWS]
    inventory, truncated = output_candidates(prompt, None, class_lookup=class_lookup, off=off)
    view["_view_inventory"] = inventory
    sampling, sampling_truncated = sampling_candidates(prompt, None, off=off, class_lookup=class_lookup)
    view["_view_sampling_index"] = sampling_index(prompt, None, class_lookup=class_lookup)
    _publish_candidates(view, truncated, sampling, sampling_truncated)


def _describe_stored(prompt: dict, identity: str, entry: dict, origin: str, class_lookup, off,
                     authored: dict | None = None) -> dict:
    path, class_type = entry["source"]["path"], entry["source"]["class_type"]
    node = prompt.get(path)
    present = isinstance(node, dict) and node.get("class_type") == class_type
    resolved = {"label": entry["label"], "order": entry["order"], "path": path, "class_type": class_type,
                "title": _title(node) if present else class_type, "status": "ok" if present else "missing"}
    if entry.get("feed") == FEED_SAMPLING:
        resolved["feed"] = FEED_SAMPLING
    if entry.get("timing") is not None:
        resolved["timing"] = entry["timing"]
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
                       **({"feed": FEED_SAMPLING} if d.get("feed") == FEED_SAMPLING else {}),
                       **({"timing": copy.deepcopy(d["timing"])} if d.get("timing") else {}),
                       **({"participation": "inactive", "inactive_reason": d["inactive_reason"]}
                          if d.get("participation") == "inactive" and d.get("inactive_reason") else {})}
        for d in rows}}


# ── Validation ───────────────────────────────────────────────────────────────

PRESENTATION_FIELDS = ("label", "order", "timing")


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
    if field == "timing":
        # None removes it: the View follows nothing again.
        if raw is None:
            return None
        if descriptor.get("feed") == FEED_SAMPLING:
            raise ControlValueError(SAMPLING_TIMING_REASON)
        timing = view_timing.clean_timing(raw)
        if timing is None:
            raise ControlValueError("This timing is not valid: choose where the output starts, "
                                    "a frame offset from 0 and the Editor's frame rate or one up to 240.")
        return timing
    if field == "reset":
        if mode != "sdk" or descriptor.get("origin") != "app_copy":
            raise ControlValueError(SDK_RESET_REASON)
        if raw is not True:
            raise ControlValueError("Send true to reset.")
        return True
    raise ControlValueError("This target is unavailable. Refresh the Controls panel.")


def validate_structural(view: dict, identity: str, op: str, raw: Any, mode: str | None, state: dict, next_order) -> dict:
    """``view:<id>:create {candidate: {path}, label?, feed?}`` and
    ``view:<id>:remove true``. ``state`` carries this batch's claims. The mounted
    pre-check (``relay``) checks shape only: the canvas writer revalidates.
    One View per node path, whatever its feed."""
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
    feed = raw.get("feed", FEED_OUTPUT) if isinstance(raw, dict) else FEED_OUTPUT
    if feed not in (FEED_OUTPUT, FEED_SAMPLING):
        raise ControlValueError("Choose an output or a sampling preview.")
    what = "a node" if feed == FEED_SAMPLING else "an output"
    if not isinstance(raw, dict) or not isinstance(raw.get("candidate"), dict):
        raise ControlValueError(f"Choose {what}.")
    path = cm.clean_path(raw["candidate"].get("path"))
    if path is None:
        raise ControlValueError(f"Choose {what}.")
    label = raw.get("label")
    if label is not None and (not isinstance(label, str) or len(label.strip()) > PANEL_LABEL_MAX_CHARS):
        raise ControlValueError("A label must be text of at most 120 characters.")
    if path in state.setdefault("claimed", set()):
        raise ControlValueError("Two changes add the same node." if feed == FEED_SAMPLING
                                else "Two changes add the same output.")
    state["claimed"].add(path)
    if shape_only:
        return {"candidate": {"path": path}, "label": (label or "").strip(), "feed": feed}
    if existing is not None:
        if existing.get("path") == path:
            return {"noop": True}  # a retry of a create that landed
        raise ControlValueError("This View id is already in use.")
    if feed == FEED_SAMPLING:
        class_type = (view.get("_view_sampling_index") or {}).get(path)
        if class_type is None:
            if any(c["path"] == path for c in view.get("_view_inventory", [])):
                raise ControlValueError("This node is an output node: give it an output View instead.")
            # The canvas sends bodies only for some muted nodes: an off node may simply be unseen.
            raise ControlValueError("This node is switched off or no longer in the workflow. Refresh the Controls panel.")
        host, rel = anchor_of(path)
        found = {"path": path, "class_type": class_type, "host": host, "rel": rel}
    else:
        found = next((c for c in view.get("_view_inventory", []) if c["path"] == path), None)
    if found is None:
        raise ControlValueError("This output is no longer in the workflow. Refresh the Controls panel.")
    if any(d.get("path") == path for d in view.get("views", []) or []):
        raise ControlValueError("This node already has a View." if feed == FEED_SAMPLING
                                else "This output already has a View.")
    count = state.setdefault("count", len(view.get("views", []) or []))
    if count >= cm.MAX_VIEWS:
        raise ControlValueError(f"At most {cm.MAX_VIEWS} Views fit in one workflow.")
    state["count"] = count + 1
    return {"candidate": {key: found[key] for key in ("path", "class_type", "host", "rel")},
            "label": (label or "").strip(), "order": next_order(), "feed": feed}


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
        if value.get("feed") == FEED_SAMPLING:
            views[identity]["feed"] = FEED_SAMPLING
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
        # The copy stands alone: it keeps the View's timing and feed too.
        if current.get("timing"):
            entry["timing"] = copy.deepcopy(current["timing"])
        if current.get("feed") == FEED_SAMPLING:
            entry["feed"] = FEED_SAMPLING
    if field == "timing" and value is None:
        entry.pop("timing", None)
    else:
        entry[field] = value
