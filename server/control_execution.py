"""Execution Controls: a root node, subgraph instance or canvas group switched
Enabled, Muted or Bypassed from Controls.

The manifest (``control_manifest.read_execution``) owns what is curated; the
native graph owns every node's mode. The canvas resolver
(``web/js/control_bindings.js`` ``resolveExecution``) exports each Control's
members and their modes; nothing here trusts that export blindly: a member the
export calls enabled must be in the prompt with its class, and one it calls off
must be absent, because the native serializer omits muted and bypassed nodes.

Pure and standard-library only, like the other Controls modules.
"""

from __future__ import annotations

from typing import Any

from . import control_manifest as cm
from . import control_routing

SOURCE = "execution"
MIXED = "mixed"
# Nodes whose own Controls would switch themselves off. Never a target, and
# never inside a group that is one.
INFRASTRUCTURE_CLASSES = frozenset({"SonderEditor", cm.RELAY_CLASS_TYPE})
MAX_CANDIDATES = 500

REASONS = {
    "missing": "This node or group is no longer in the workflow. Rebind or remove it.",
    "duplicate": "Two nodes carry this switch. Remove one of them.",
    "stale": "This switch changed since Controls last read it. Refresh the Controls panel.",
    "infrastructure": "It holds the Sonder Editor or the Control Relay, which must stay on.",
    "too_many": f"It holds more than {cm.MAX_EXECUTION_MEMBERS} nodes. Split the group.",
    "empty": "The group holds no nodes.",
    "kind": "This node moved into or out of a subgraph. Rebind it.",
    "virtual": "This node never runs (a reroute or a note), so it can't be switched.",
    "budget": "The workflow's switches cover more than 512 nodes in all. Remove a switch or split a group.",
    "unsupported": "This can't be switched from Controls.",
}
SDK_REASON = "Switch these on the canvas for now."
# What keeps a switch from working in the Sonder App.
SDK_STRUCTURAL_REASON = "Add, remove and rebind node switches on the canvas."
OFF_AT_EXPORT_REASON = ("Its nodes were off when this workflow was exported. "
                        "Enable them on the canvas and export again to switch it here.")
REEXPORT_REASON = "Export this workflow again from the canvas with this version of Sonder to switch it here."
ROUTING_REASONS = {
    "missing": REEXPORT_REASON,
    "budget": control_routing.ERRORS["budget"] + " Switch it on the canvas.",
    "unsupported": control_routing.ERRORS["unsupported"] + " Switch it on the canvas.",
}
MODE_LABELS = {"enabled": "Enabled", "muted": "Muted", "bypassed": "Bypassed"}


def _instance_present(prompt: dict, path: str) -> bool:
    prefix = f"{path}:"
    return any(isinstance(key, str) and key.startswith(prefix) for key in prompt)


def _holds_infrastructure(prompt: dict, member: dict) -> bool:
    """An instance whose inner nodes include the Editor or the Relay."""
    if member.get("kind") != "instance":
        return False
    prefix = f"{member['path']}:"
    return any(isinstance(key, str) and key.startswith(prefix) and isinstance(node, dict)
               and node.get("class_type") in INFRASTRUCTURE_CLASSES for key, node in prompt.items())


def _member_consistent(prompt: dict, member: dict) -> bool:
    """A member's exported mode agrees with the prompt the same export made."""
    if member.get("kind") == "instance":
        # An instance never reaches a prompt itself, only its inner nodes do;
        # an enabled one may still have every inner node muted on its own.
        return member["mode"] == "enabled" or not _instance_present(prompt, member["path"])
    node = prompt.get(member["path"])
    if member["mode"] == "enabled":
        return isinstance(node, dict) and node.get("class_type") == member["class_type"]
    return not isinstance(node, dict)


def combined_mode(members: list[dict]) -> str | None:
    modes = {member["mode"] for member in members}
    if not modes:
        return None
    return modes.pop() if len(modes) == 1 else MIXED


def default_label(entry: dict) -> str:
    title = entry.get("title") or ""
    if not title and entry.get("members"):
        first = entry["members"][0]
        title = first.get("title") or first.get("class_type") or ""
    return title or ("Group" if entry["target"]["kind"] == "group" else "Node")


def describe_one(prompt: dict, identity: str, entry: dict) -> dict:
    kind = entry["target"]["kind"]
    members = entry.get("members") or []
    descriptor: dict[str, Any] = {
        "source": SOURCE,
        "execution_id": identity,
        "origin": "authored",
        "label": entry["label"] or default_label(entry),
        # What an emptied app label falls back to.
        "default_label": default_label(entry),
        "group": entry["group"],
        "order": entry["order"],
        "advanced": entry["advanced"],
        "note": entry["note"],
        "kind": kind,
        **({"group_id": entry["target"]["group_id"]} if kind == "group" else {}),
        "members": [{"path": m["path"], "title": m["title"] or m["class_type"], "mode": m["mode"],
                     "kind": m.get("kind", "node"), "class_type": m["class_type"]} for m in members],
        "member_count": entry.get("member_count", len(members)),
        "mode": None,
        "value": None,
        "reason": None,
    }
    status = entry.get("status") or "missing"
    problem = entry.get("problem")
    if status == "ok":
        if not members:
            status, problem = "unsupported", "empty"
        elif descriptor["member_count"] > cm.MAX_EXECUTION_MEMBERS or len(members) > cm.MAX_EXECUTION_MEMBERS:
            status, problem = "unsupported", "too_many"
        elif kind != "group" and len(members) != 1:
            status, problem = "missing", "stale"
        elif any(m["class_type"] in INFRASTRUCTURE_CLASSES or _holds_infrastructure(prompt, m) for m in members):
            status, problem = "unsupported", "infrastructure"
        elif kind != "group" and members[0].get("kind", "node") != kind:
            status, problem = "unsupported", "kind"
        elif not all(_member_consistent(prompt, m) for m in members):
            status, problem = "missing", "stale"
    descriptor["status"] = status
    if status == "ok":
        descriptor["mode"] = descriptor["value"] = combined_mode(members)
        return descriptor
    if problem:
        descriptor["problem"] = problem
    descriptor["reason"] = REASONS.get(problem) or REASONS.get(status) or REASONS["unsupported"]
    return descriptor


def describe_execution(prompt: dict, manifest: dict) -> tuple[list[dict], str | None]:
    """Descriptors for the manifest's execution Controls, and a section error."""
    controls = manifest.get("execution") or {}
    rows = [describe_one(prompt, identity, entry) for identity, entry in controls.items()]
    # Two Controls that name one node or group each stay readable: overlap is
    # allowed (the last action wins), but one anchor may not claim two ids.
    rows.sort(key=lambda d: (d["order"], d["label"].lower(), d["execution_id"]))
    return rows, manifest.get("execution_error")


# ── The Sonder App ───────────────────────────────────────────────────────────

def stored_node_modes(stored: dict) -> dict:
    """The sidecar's ``node_modes``: ``{root path: {mode: muted|bypassed,
    class_type}}``. Enabled is absence. The class is the node's identity, as a
    stored value's signature is: after a re-import renumbers nodes, a mode
    never lands on another kind of node. Anything else is preserved on disk
    and ignored here."""
    raw = stored.get("node_modes") if isinstance(stored, dict) else None
    return {path: (entry["mode"], entry["class_type"]) for path, entry in (raw.items() if isinstance(raw, dict) else [])
            if cm._root_path(path) is not None and isinstance(entry, dict)
            and entry.get("mode") in control_routing.OFF_MODES and isinstance(entry.get("class_type"), str)}


def apply_sdk_state(view: dict, prompt: dict, stored: dict, hidden: set) -> dict:
    """Make the app's switches match its sidecar; return the node modes a run
    applies. A switch is switchable only when the export ran every one of its
    nodes and its routing capsule covers them: the API file has no other way
    to bring a node back or to know what bypassing it routes. A hidden
    switch's stored modes are skipped (the workflow's own state runs), unless
    another visible switch covers the same node; the last action wins, as
    node modes on the canvas do."""
    manifest = cm.find_relay(prompt)["manifest"]
    routing, problem = control_routing.read_routing(manifest.get("execution_routing"))
    stored_modes = stored_node_modes(stored)
    descriptors = view.get("execution", []) or []
    effective, switchable = {}, []
    for descriptor in descriptors:
        descriptor["hidden"] = f"execution:{descriptor['execution_id']}" in hidden
        if descriptor["status"] != "ok":
            descriptor["read_only"] = True
            continue
        paths = [m["path"] for m in descriptor["members"]]
        if any(m["mode"] != "enabled" for m in descriptor["members"]):
            descriptor.update(read_only=True, reason=OFF_AT_EXPORT_REASON)
        elif routing is None or not control_routing.covers(routing, paths):
            descriptor.update(read_only=True, reason=ROUTING_REASONS.get(problem, REEXPORT_REASON))
        else:
            descriptor["read_only"] = False
            switchable.append(descriptor)
            if not descriptor["hidden"]:
                effective.update({m["path"]: stored_modes[m["path"]][0] for m in descriptor["members"]
                                  if stored_modes.get(m["path"], (None, None))[1] == m["class_type"]})
    for descriptor in switchable:
        for member in descriptor["members"]:
            member["mode"] = effective.get(member["path"], "enabled")
        descriptor["mode"] = descriptor["value"] = combined_mode(descriptor["members"])
    view["_node_modes"], view["_routing"] = effective, routing
    _mark_participation(view, effective)
    return effective


def _mark_participation(view: dict, off: dict) -> None:
    """A setting on a node the app switched off: ``inactive`` (``partial`` when
    only some of its targets are), with the canvas's reason shape."""
    if not off:
        return
    titles = {m["path"]: m.get("title") or m.get("class_type") or m["path"]
              for d in view.get("execution", []) or [] for m in d.get("members", [])}
    for descriptor in view.get("bindings", []):
        reasons = []
        for target in descriptor.get("targets", []):
            path = str(target.get("path", ""))
            root = path.split(":", 1)[0]
            if path in off:
                reasons.append({"mode": off[path], "by": "self", "label": titles.get(path, path)})
            elif root in off:
                reasons.append({"mode": off[root], "by": "parent", "label": titles.get(root, root)})
        if reasons:
            descriptor["participation"] = "inactive" if len(reasons) == len(descriptor.get("targets", [])) else "partial"
            descriptor["inactive_reason"] = reasons[0]


# ── Validation ───────────────────────────────────────────────────────────────

PRESENTATION_FIELDS = ("label", "note", "order", "group", "advanced")


def table_entries(view: dict) -> dict:
    """``execution:<id>:<field>`` targets the view accepts."""
    table = {}
    for descriptor in view.get("execution", []) or []:
        identity = descriptor.get("execution_id")
        if not isinstance(identity, str) or not identity:
            continue
        base = f"execution:{identity}"
        for field in PRESENTATION_FIELDS:
            table[f"{base}:{field}"] = (descriptor, field)
        if descriptor.get("status") == "ok":
            table[f"{base}:mode"] = (descriptor, "mode")
            if descriptor.get("kind") == "group":
                table[f"{base}:modes"] = (descriptor, "modes")
    return table


def coerce_mode(descriptor: dict, field: str, raw: Any, *, expanded: bool) -> Any:
    from .control_descriptors import ControlValueError

    if descriptor.get("read_only"):
        raise ControlValueError(SDK_REASON)
    if field == "mode":
        if raw not in cm.EXECUTION_MODES:
            raise ControlValueError("Choose Enabled, Muted or Bypassed.")
        return raw
    # ``modes`` sets each member of a group as a preset captured it. Only a
    # preset's own expansion sends it; the panel switches a group as a whole.
    if not expanded:
        raise ControlValueError("This target is unavailable. Refresh the Controls panel.")
    paths = {m["path"] for m in descriptor.get("members", [])}
    if not isinstance(raw, dict) or set(raw) != paths or any(mode not in cm.EXECUTION_MODES for mode in raw.values()):
        raise ControlValueError("The group's nodes changed. Update the preset.")
    return dict(raw)


def effects_of(descriptor: dict, field: str, value: Any) -> dict:
    """``{path: mode}`` a validated mode change sets."""
    if field == "modes":
        return dict(value)
    return {m["path"]: value for m in descriptor.get("members", [])}


def clean_candidates(raw: Any) -> list[dict]:
    """Execution candidates a canvas sent, shape-checked. Members keep only what
    the server needs to refuse infrastructure and oversized groups."""
    from .control_descriptors import ControlValueError

    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > MAX_CANDIDATES * 4:
        raise ControlValueError("Too many node candidates.")
    found = []
    for item in raw:
        if not isinstance(item, dict) or item.get("kind") not in cm.EXECUTION_KINDS:
            raise ControlValueError("A node candidate is invalid.")
        entry = {"kind": item["kind"], "title": item.get("title")[:cm.LABEL_MAX] if isinstance(item.get("title"), str) else ""}
        if item["kind"] == "group":
            group_id = item.get("group_id")
            if isinstance(group_id, bool) or not isinstance(group_id, int) or abs(group_id) > 2 ** 31:
                raise ControlValueError("A group candidate is invalid.")
            entry["group_id"] = group_id
        else:
            path = cm._root_path(item.get("path"))
            if path is None:
                raise ControlValueError("A node candidate is invalid.")
            entry["path"] = path
        classes = item.get("classes")
        entry["classes"] = [c[:256] for c in classes[:cm.MAX_EXECUTION_MEMBERS + 1] if isinstance(c, str)] if isinstance(classes, list) else []
        count = item.get("member_count")
        entry["member_count"] = count if isinstance(count, int) and not isinstance(count, bool) and count >= 0 else len(entry["classes"])
        owner = item.get("anchored_by")
        entry["anchored_by"] = owner if isinstance(owner, str) and cm.ID_RE.fullmatch(owner) else None
        found.append(entry)
    return found


def candidate_key(candidate: dict) -> tuple:
    return (candidate["kind"], candidate.get("group_id") if candidate["kind"] == "group" else candidate.get("path"))


def candidate_problem(candidate: dict) -> str | None:
    if any(c in INFRASTRUCTURE_CLASSES for c in candidate["classes"]):
        return "infrastructure"
    if candidate["kind"] == "group":
        if candidate["member_count"] == 0:
            return "empty"
        if candidate["member_count"] > cm.MAX_EXECUTION_MEMBERS:
            return "too_many"
    return None


def manage_candidates(inventory: list[dict], view: dict, query: str = "") -> tuple[list[dict], bool]:
    """What Manage lists under Nodes and groups: allowed candidates first, then
    refused ones a filter names, each with its reason. A node already carrying
    a switch's anchor, or a group a switch names, is not listed again."""
    taken_groups = {d.get("group_id") for d in view.get("execution", []) or [] if d.get("kind") == "group"}
    q = str(query or "").strip().lower()
    listed, refused = [], []
    for candidate in inventory:
        if candidate["anchored_by"] or (candidate["kind"] == "group" and candidate["group_id"] in taken_groups):
            continue
        if q and q not in candidate["title"].lower() and not any(q in c.lower() for c in candidate["classes"][:1]):
            continue
        problem = candidate_problem(candidate)
        row = {key: candidate[key] for key in ("kind", "title", "member_count") if key in candidate}
        row.update({"path": candidate["path"]} if candidate["kind"] != "group" else {"group_id": candidate["group_id"]})
        if problem:
            if q:
                refused.append({**row, "reason": REASONS[problem]})
            continue
        listed.append(row)
    truncated = len(listed) > MAX_CANDIDATES
    return listed[:MAX_CANDIDATES] + refused[:20], truncated


def validate_structural(view: dict, inventory: list[dict] | None, identity: str, op: str, raw: Any,
                        mode: str | None, state: dict, next_order) -> dict:
    """``execution:<id>:expose|remove|rebind``. ``state`` carries this batch's
    claims, so two changes cannot expose the same node or one id twice;
    ``next_order`` is the batch's panel order for a new row. The mounted
    pre-check (``relay``) checks shape only: the canvas writer rechecks it."""
    from .control_descriptors import ControlValueError, PANEL_LABEL_MAX_CHARS

    if mode == "sdk":
        raise ControlValueError(SDK_STRUCTURAL_REASON)
    if mode not in ("canvas", "relay"):
        raise ControlValueError("This target is unavailable. Refresh the Controls panel.")
    if view.get("execution_error"):
        raise ControlValueError(view["execution_error"])
    existing = {d["execution_id"]: d for d in view.get("execution", []) or []}.get(identity)
    shape_only = mode == "relay"
    if op == "remove":
        if raw is not True:
            raise ControlValueError("Send true to remove.")
        return {"remove": True} if existing is not None or shape_only else {"noop": True}
    if op not in ("expose", "rebind"):
        raise ControlValueError("This target is unavailable. Refresh the Controls panel.")
    if not isinstance(raw, dict) or not isinstance(raw.get("candidate"), dict):
        raise ControlValueError("Choose a node or group.")
    label = raw.get("label")
    if label is not None and (not isinstance(label, str) or len(label.strip()) > PANEL_LABEL_MAX_CHARS):
        raise ControlValueError("A label must be text of at most 120 characters.")
    shaped = clean_candidates([raw["candidate"]])[0]
    locator = {"kind": shaped["kind"], **({"group_id": shaped["group_id"]} if shaped["kind"] == "group" else {"path": shaped["path"]})}
    if candidate_key(shaped) in state.setdefault("claimed", set()):
        raise ControlValueError("Two changes switch the same node or group.")
    state["claimed"].add(candidate_key(shaped))
    if not shape_only:
        found = {candidate_key(c): c for c in inventory or []}.get(candidate_key(shaped))
        if found is None:
            raise ControlValueError("This node or group is no longer in the workflow. Refresh the Controls panel.")
        if found["anchored_by"] and found["anchored_by"] != identity:
            raise ControlValueError("This node is already a switch.")
        if problem := candidate_problem(found):
            raise ControlValueError(REASONS[problem])
        if shaped["kind"] == "group" and any(d.get("kind") == "group" and d.get("group_id") == shaped["group_id"]
                                             and d["execution_id"] != identity for d in view.get("execution", []) or []):
            raise ControlValueError("This group is already a switch.")
    if op == "rebind":
        if existing is None and not shape_only:
            raise ControlValueError("This switch is no longer in the workflow.")
        if existing is not None and existing.get("status") == "ok" and not shape_only:
            raise ControlValueError("Only a switch that is off can be rebound.")
        return {"candidate": locator}
    if existing is not None:
        if shape_only:
            return {"candidate": locator, "label": (label or "").strip()}
        # A retry of an expose that landed changes nothing.
        same = (existing.get("kind") == shaped["kind"] and (
            existing.get("group_id") == shaped.get("group_id") if shaped["kind"] == "group"
            else [m["path"] for m in existing.get("members") or []] == [shaped["path"]]))
        if same:
            return {"noop": True}
        raise ControlValueError("This Control id is already in use.")
    count = state.setdefault("count", len(view.get("execution", []) or []))
    if count >= cm.MAX_EXECUTION:
        raise ControlValueError(f"At most {cm.MAX_EXECUTION} node switches fit in one workflow.")
    state["count"] = count + 1
    return {"candidate": locator, "label": (label or "").strip(), "order": next_order()}


# ── Export readiness and Sonder App reasons ──────────────────────────────────

def export_readiness(view: dict) -> dict:
    """What Export (API) would leave out: settings and switches whose nodes are
    off now. Read-only advice; Sonder never enables a node for the author."""
    settings = [{"binding_id": d["binding_id"], "label": d.get("label") or d["binding_id"],
                 "mode": (d.get("inactive_reason") or {}).get("mode", "muted")}
                for d in view.get("bindings", []) if d.get("participation") in ("inactive", "partial")]
    switches = [{"execution_id": d["execution_id"], "label": d.get("label") or d["execution_id"],
                 "mode": d.get("mode")}
                for d in view.get("execution", []) or []
                if d.get("status") == "ok" and any(m["mode"] != "enabled" for m in d.get("members", []))]
    return {"settings": settings, "execution": switches}


def mode_summary(descriptor: dict) -> str:
    """``Muted``, or ``Mixed (2 of 5 off)`` for a group whose members differ."""
    mode = descriptor.get("mode")
    if mode == MIXED:
        off = sum(1 for m in descriptor.get("members", []) if m["mode"] != "enabled")
        return f"Mixed ({off} of {len(descriptor.get('members', []))} off)"
    return MODE_LABELS.get(mode, "")
