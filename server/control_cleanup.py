"""Explicit recovery-state cleanup; eligibility is derived, never persisted."""
import copy
import json

from . import control_manifest as cm

CATEGORIES = ("bindings", "suggestions", "definition_anchors", "values", "hidden", "execution", "preset_members")


def valid_id(value):
    return isinstance(value, str) and cm.ID_RE.fullmatch(value) and value not in cm.FORBIDDEN_KEYS


def inventory(view, *, stored=None, definition_anchors=()):
    """The same inventory feeds review and fresh write validation in every host."""
    result = {key: [] for key in CATEGORIES}
    if view.get("error") or (view.get("relay") or {}).get("status") != "ok":
        return result
    bindings = {b["binding_id"]: b for b in view.get("bindings", [])}
    switches = {d["execution_id"]: d for d in view.get("execution", []) or []}
    sdk = stored is not None
    restorable = {row["binding_id"] for row in (view.get("restore") or {}).get("restorable", [])}
    # Only a setting that can't be resolved is missing; one on a muted or
    # bypassed node is inactive, keeps its binding and is never offered here.
    for bid, binding in bindings.items():
        if bid not in restorable and binding.get("status") == "missing" and (not sdk or binding.get("origin") == "local"):
            result["bindings"].append({"id": bid, "label": binding.get("label") or bid})
    if not sdk:  # switches are authored on the canvas; the app never removes them
        # A stale switch (its export and the prompt disagree for a moment) is
        # not gone: the canvas still resolves it, so it is never offered.
        result["execution"] = [{"id": eid, "label": d.get("label") or eid}
                               for eid, d in switches.items() if d.get("status") == "missing" and d.get("problem") != "stale"]
    if sdk:
        known = {f"binding:{bid}" for bid in bindings}
        known.update(f"note:{n['note_id']}" for n in view.get("notes", []))
        for key in stored.get("hidden", {}):
            parts = key.split(":")
            if len(parts) == 2 and parts[0] in ("binding", "note") and valid_id(parts[1]) and key not in known:
                result["hidden"].append({"id": key, "label": key})
        for bid, entry in stored.get("bindings", {}).items():
            if valid_id(bid) and bid not in bindings:
                preview = json.dumps(entry, ensure_ascii=False, default=str)
                result["values"].append({"id": bid, "label": bid, "preview": preview[:1000]})
    else:
        result["suggestions"] = [{"id": s["binding_id"], "label": s.get("label") or s["binding_id"]}
                                 for s in view.get("suggestions", [])]
        root_ids = {s["id"] for s in result["suggestions"]}
        result["definition_anchors"] = [{"id": bid, "label": bid} for bid in dict.fromkeys(definition_anchors)
                                         if valid_id(bid) and bid not in bindings and bid not in root_ids]
    removable = {r["id"] for key in ("bindings", "suggestions", "definition_anchors") for r in result[key]}
    gone = {r["id"] for r in result["execution"]}
    for pid, preset in view.get("_presets", {}).items():
        for bid in preset["members"]:
            if bid not in bindings or bid in removable:
                result["preset_members"].append({"preset_id": pid, "binding_id": bid,
                    **({"requires_binding": bid} if bid in bindings else {})})
        # The app never removes a switch, but a preset member whose switch is
        # gone (an app export leaves hidden ones out) is still repairable here.
        for eid in preset.get("execution") or {}:
            if eid not in switches or eid in gone:
                result["preset_members"].append({"preset_id": pid, "execution_id": eid,
                    **({"requires_execution": eid} if eid in switches else {})})
    return result


def _member_key(row):
    return (row["preset_id"], row.get("binding_id"), row.get("execution_id"))


def validate(view, raw, *, mode):
    from .control_descriptors import ControlValueError
    if mode not in ("canvas", "sdk", "relay") or view.get("error") or (view.get("relay") or {}).get("status") != "ok":
        raise ControlValueError("Fix the Sonder Control Relay before cleaning up unavailable Controls.")
    if not isinstance(raw, dict) or set(raw) - set(CATEGORIES):
        raise ControlValueError("Choose the unavailable Controls to clean up.")
    cleaned = {}
    for key in CATEGORIES:
        rows = raw.get(key, [])
        if not isinstance(rows, list) or len(rows) > 4096:
            raise ControlValueError("The cleanup selection is invalid or too large.")
        cleaned[key] = []
        for row in rows:
            if key == "preset_members":
                good = (isinstance(row, dict) and set(row) in ({"preset_id", "binding_id"}, {"preset_id", "execution_id"})
                        and all(valid_id(v) for v in row.values()))
            elif key == "hidden":
                parts = row.split(":") if isinstance(row, str) else []
                good = len(parts) == 2 and parts[0] in ("binding", "note") and valid_id(parts[1])
            else:
                good = valid_id(row)
            if not good or row in cleaned[key]:
                raise ControlValueError("The cleanup selection is invalid.")
            cleaned[key].append(copy.deepcopy(row))
    if not any(cleaned.values()):
        raise ControlValueError("Select at least one item to clean up.")
    if mode == "relay":
        return cleaned  # published lists may be capped; the canvas rechecks live
    available = view.get("cleanup") or {}
    for key in CATEGORIES[:-1]:
        eligible = {row["id"] for row in available.get(key, [])}
        if any(identity not in eligible for identity in cleaned[key]):
            raise ControlValueError("An item is no longer unavailable. Reopen the cleanup review.")
    members = {_member_key(r): r for r in available.get("preset_members", [])}
    for row in cleaned["preset_members"]:
        found = members.get(_member_key(row))
        if (found is None or (found.get("requires_binding") and found["requires_binding"] not in cleaned["bindings"])
                or (found.get("requires_execution") and found["requires_execution"] not in cleaned["execution"])):
            raise ControlValueError("A preset member is no longer eligible. Reopen the cleanup review.")
    return cleaned


def restore_inventory(view, raw, candidates):
    """Verify a bounded canvas hint plan with the same candidate authority as Rebind."""
    from .control_descriptors import ControlValueError, _StructuralBatch
    result = {"restorable": [], "unmatched": []}
    if (not isinstance(raw, dict) or set(raw) != set(result)
            or any(not isinstance(raw[key], list) for key in result)
            or sum(len(raw[key]) for key in result) > cm.MAX_BINDINGS):
        raise ControlValueError("Invalid Controls restore plan.")
    bindings = {b["binding_id"]: b for b in view.get("bindings", [])}
    batch = _StructuralBatch(view, "canvas", candidates or [])
    seen = set()
    for key in result:
        for row in raw[key]:
            bid = row.get("binding_id") if isinstance(row, dict) else None
            if not valid_id(bid) or bid in seen:
                raise ControlValueError("Invalid Controls restore plan.")
            seen.add(bid)
            binding = bindings.get(bid)
            if not binding or binding.get("status") != "missing":
                continue
            common = {"binding_id": bid, "label": (binding.get("label") or bid)[:cm.LABEL_MAX]}
            if key == "unmatched":
                reason = row.get("reason")
                if reason not in ("several_settings", "not_found", "ambiguous", "taken", "unavailable"):
                    raise ControlValueError("Invalid Controls restore reason.")
                result[key].append({**common, "reason": reason})
                continue
            try:
                chosen = batch._candidate(row, bid)
            except ControlValueError as exc:
                result["unmatched"].append({**common, "reason": str(exc)[:500]})
            else:
                result[key].append({**common, **chosen["candidate"]})
    return result


def validate_restore(view, raw, *, mode):
    from .control_descriptors import ControlValueError
    if mode == "sdk":
        raise ControlValueError("Restore applies to canvas workflows.")
    if mode not in ("canvas", "relay") or view.get("error") or (view.get("relay") or {}).get("status") != "ok":
        raise ControlValueError("Fix the Sonder Control Relay before restoring Controls.")
    ids = raw.get("bindings") if isinstance(raw, dict) else None
    if (not isinstance(raw, dict) or set(raw) != {"bindings"} or not isinstance(ids, list)
            or not 1 <= len(ids) <= cm.MAX_BINDINGS or not all(valid_id(bid) for bid in ids)
            or len(set(ids)) != len(ids)):
        raise ControlValueError("Choose the Controls to restore.")
    if mode != "relay":
        eligible = {r["binding_id"] for r in (view.get("restore") or {}).get("restorable", [])}
        if any(bid not in eligible for bid in ids):
            raise ControlValueError("A Control can no longer be restored. Refresh the Controls panel.")
    return {"bindings": list(ids)}


def remove_members(presets, members):
    """Edit only selected members, preserving unknown metadata on retained entries."""
    changed = set()
    for row in members:
        pid = row["preset_id"]
        preset = presets.get(pid)
        table = "execution" if "execution_id" in row else "members"
        if isinstance(preset, dict) and isinstance(preset.get(table), dict):
            preset[table].pop(row.get("execution_id") or row.get("binding_id"), None)
            changed.add(pid)
            if not preset.get("members") and not preset.get("execution"):
                presets.pop(pid)
    return changed
