"""Workflow preset projection and intent validation; no persistence authority."""
from __future__ import annotations

import copy

from . import control_manifest as cm
from . import control_descriptors as cd
from . import control_rows


def effective_presets(authored, overlays):
    result = copy.deepcopy(cm.read_presets(authored))
    origins = {identity: "workflow" for identity in result}
    for identity, entry in (overlays.items() if isinstance(overlays, dict) else []):
        if isinstance(entry, dict) and entry.get("removed") is True:
            result.pop(identity, None)
            origins.pop(identity, None)
        elif identity in cm.read_presets({identity: entry}):
            result[identity] = copy.deepcopy(cm.read_presets({identity: entry})[identity])
            origins[identity] = "app_copy" if identity in (authored or {}) else "app"
    return result, origins


def _expand(view, preset):
    bindings = {d["binding_id"]: d for d in view.get("bindings", [])}
    changes, unavailable = {}, []
    for identity, member in preset["members"].items():
        descriptor = bindings.get(identity, {})
        if descriptor.get("status") != "ok" or descriptor.get("hidden") or view.get("error"):
            unavailable.append(identity)
            continue
        changes[f"binding:{identity}:value"] = member["value"]
        if descriptor.get("kind") == "SEED" and descriptor.get("seed_mode") is not None and "seed_mode" in member:
            changes[f"binding:{identity}:seed_mode"] = member["seed_mode"]
    return changes, unavailable


def _reason(view, identities):
    labels = {d["binding_id"]: d.get("label") or d["binding_id"] for d in view.get("bindings", [])}
    return "Unavailable Controls: " + ", ".join(labels.get(identity, identity) for identity in identities) + ". Repair them in Manage."


def _refused_reason(view, rejected):
    labels = {d["binding_id"]: d.get("label") or d["binding_id"] for d in view.get("bindings", [])}
    return " ".join(f"{labels.get((cd.parse_target(r['target']) or ('', '', ''))[1], r['target'])}: {r['reason']}" for r in rejected)


def _preview(value, descriptor):
    """A member's value as text; a row list reads as its summary (``3 LoRAs · 2 on``)."""
    if isinstance(value, list):
        return control_rows.summary(value, descriptor if descriptor.get("kind") == control_rows.ROWS_KIND else {})[:200]
    return ("true" if value is True else "false" if value is False else "null" if value is None else str(value))[:200]


def preset_states(view, presets, *, origins=None, prompt=None):
    """One pure match authority, called after each host's effective projection."""
    bindings = {d["binding_id"]: d for d in view.get("bindings", [])}
    rows = []
    for identity, preset in sorted(presets.items(), key=lambda item: item[1]["order"]):
        changes, unavailable = _expand(view, preset)
        cleaned, rejected = cd.validate_binding_changes(view, changes, prompt=prompt) if not unavailable else ({}, [])
        state = "unavailable" if unavailable else "refused" if rejected else "match"
        members = []
        for bid, member in preset["members"].items():
            descriptor = bindings.get(bid, {})
            status = "unavailable" if bid in unavailable else "ok"
            members.append({"binding_id": bid, "preview": _preview(member["value"], descriptor), "status": status,
                            **({"seed_mode": member["seed_mode"]} if "seed_mode" in member else {}),
                            # Two row lists can share a summary; Manage's hint needs the real comparison.
                            **({"differs": control_rows.canonical(member["value"]) != control_rows.canonical(descriptor.get("value"))}
                               if descriptor.get("kind") == control_rows.ROWS_KIND else {})})
            if state != "match":
                continue
            mode = member.get("seed_mode") if descriptor.get("seed_mode") is not None else None
            if mode is not None and mode != descriptor.get("seed_mode"):
                state = "differs"
            elif mode in (None, "fixed"):
                try:
                    if cd.coerce_binding_value(descriptor, descriptor.get("value")) != cleaned[f"binding:{bid}:value"]:
                        state = "differs"
                except (cd.ControlValueError, KeyError):
                    state = "differs"
        rows.append({"id": identity, "name": preset["name"], "description": preset["description"],
                     "order": preset["order"], "origin": (origins or {}).get(identity, "workflow"),
                     "state": state, "members": members, "unavailable": unavailable,
                     "reason": _reason(view, unavailable) if unavailable else _refused_reason(view, rejected) if rejected else ""})
    return rows


def _capture(view, members, previous=None):
    if not isinstance(members, list) or not members or len(set(members)) != len(members):
        raise cd.ControlValueError("Choose at least one Control, without duplicates.")
    bindings = {d["binding_id"]: d for d in view.get("bindings", [])}
    result = {}
    for bid in members:
        if not isinstance(bid, str) or not cm.ID_RE.fullmatch(bid) or bid in cm.FORBIDDEN_KEYS:
            raise cd.ControlValueError("A preset member id is invalid.")
        descriptor = bindings.get(bid, {})
        if descriptor.get("status") != "ok" or descriptor.get("hidden"):
            if previous and bid in previous["members"]:
                result[bid] = copy.deepcopy(previous["members"][bid])
                continue
            raise cd.ControlValueError(_reason(view, [bid]))
        value = cd.coerce_binding_value(descriptor, descriptor.get("value"))
        result[bid] = {"value": str(value) if descriptor.get("kind") in ("INT", "SEED") else value}
        if descriptor.get("kind") == "SEED" and descriptor.get("seed_mode") in cm.SEED_MODES:
            result[bid]["seed_mode"] = descriptor["seed_mode"]
    if sum(1 + ("seed_mode" in entry) for entry in result.values()) > cm.MAX_PRESET_TARGETS:
        raise cd.ControlValueError("A preset may hold at most 64 targets; a seed with a mode counts as two.")
    return result


def validate_preset_changes(view, changes, *, prompt=None, structural=None, inventory=None):
    """Return canonical preset edits or an ordinary atomic value/mode batch.

    Mounted structural intents are shape-checked only and forwarded raw. The
    private definitions never travel in its bounded publication view.
    """
    definitions = copy.deepcopy(view.get("_presets", {}))
    ordinary = {target: raw for target, raw in changes.items() if (cd.parse_target(target) or (None,))[0] != "preset"}
    cleaned, rejected = cd.validate_binding_changes(view, ordinary, prompt=prompt, structural=structural, inventory=inventory) if ordinary else ({}, [])
    for target, raw in changes.items():
        parsed = cd.parse_target(target)
        if not parsed or parsed[0] != "preset":
            continue
        _, identity, field = parsed
        try:
            if identity in cm.FORBIDDEN_KEYS or structural not in ("canvas", "sdk", "relay"):
                raise cd.ControlValueError("This preset target is unavailable.")
            if view.get("error") or (view.get("relay") or {}).get("status") == "error":
                raise cd.ControlValueError("The Sonder Control Relay cannot be read.")
            if field == "apply" and (len(changes) != 1 or raw is not True):
                raise cd.ControlValueError("Apply one preset by itself with true.")
            if field in ("create", "capture"):
                if not isinstance(raw, dict) or not isinstance(raw.get("members"), list):
                    raise cd.ControlValueError("Choose the Controls to capture.")
                if not raw["members"] or len(raw["members"]) > 64 or any(not isinstance(x, str) for x in raw["members"]):
                    raise cd.ControlValueError("Choose 1 to 64 Controls.")
            if field in ("remove", "reset") and raw is not True:
                raise cd.ControlValueError(f"Send true to {field} a preset.")
            if field == "reset" and structural != "sdk":
                raise cd.ControlValueError("Reset to the workflow's version is available in the Sonder App.")
            if field == "move" and (not isinstance(raw, dict) or (raw.get("before") is not None and not isinstance(raw["before"], str))):
                raise cd.ControlValueError("Choose a preset to move before.")
            if field == "name" and isinstance(raw, str):
                raw = raw.strip()  # stored trimmed; names saved earlier are not rewritten
            if field in ("name", "description"):
                limit = cm.LABEL_MAX if field == "name" else cm.DESCRIPTION_MAX
                if not isinstance(raw, str) or len(raw) > limit or (field == "name" and not raw.strip()):
                    raise cd.ControlValueError(f"A preset {field} must be text of 1–{limit} characters." if field == "name" else f"A preset description may hold at most {limit} characters.")
            if field not in {"create", "capture", "apply", "name", "description", "move", "remove", "reset"}:
                raise cd.ControlValueError("This preset target is unavailable.")
            if structural == "relay":
                cleaned[target] = copy.deepcopy(raw)
                continue
            existing = definitions.get(identity)
            if field == "apply":
                if not existing:
                    raise cd.ControlValueError("This preset no longer exists. Open Manage.")
                expanded, unavailable = _expand(view, existing)
                if unavailable:
                    raise cd.ControlValueError(_reason(view, unavailable))
                values, errors = cd.validate_binding_changes(view, expanded, prompt=prompt)
                if errors:
                    raise cd.ControlValueError(_refused_reason(view, errors))
                return values, []
            if field == "create":
                if existing:
                    raise cd.ControlValueError("This preset id is already in use.")
                if len(definitions) >= cm.MAX_PRESETS:
                    raise cd.ControlValueError("At most 32 presets fit in one workflow.")
                name = raw.get("name")
                entry = {"name": name.strip() if isinstance(name, str) else name, "description": raw.get("description", ""),
                         "order": min(cm.ORDER_LIMIT, max((p["order"] for p in definitions.values()), default=-1) + 1),
                         "members": _capture(view, raw["members"])}
                if identity not in cm.read_presets({identity: entry}):
                    raise cd.ControlValueError("A preset needs a name of 1–120 characters and a description of at most 1000 characters.")
                definitions[identity] = entry
                cleaned[target] = entry
                continue
            if not existing and field not in ("remove", "reset"):
                raise cd.ControlValueError("This preset no longer exists. Open Manage.")
            if field == "capture":
                entry = {**existing, "members": _capture(view, raw["members"], existing)}
                definitions[identity] = entry
                cleaned[target] = entry
            elif field in ("name", "description"):
                definitions[identity][field] = raw
                cleaned[target] = raw
            elif field == "move":
                before = raw.get("before")
                if before == identity or (before is not None and before not in definitions):
                    raise cd.ControlValueError("Choose another preset to move before.")
                cleaned[target] = {"before": before}
            else:
                cleaned[target] = True
                definitions.pop(identity, None)
        except (cd.ControlValueError, TypeError, ValueError) as exc:
            rejected.append({"target": target, "reason": str(exc)})
    return ({} if rejected else cleaned), rejected


def edit_definitions(definitions, identity, field, value):
    """Apply a validated edit to an ephemeral copy owned by its writer."""
    if field in ("create", "capture"):
        definitions[identity] = copy.deepcopy(value)
    elif field == "remove":
        definitions.pop(identity, None)
    elif field in ("name", "description"):
        definitions[identity][field] = value
    elif field == "move":
        ordered = sorted(definitions, key=lambda key: definitions[key]["order"])
        ordered.remove(identity)
        before = value.get("before")
        ordered.insert(ordered.index(before) if before else len(ordered), identity)
        for order, key in enumerate(ordered):
            definitions[key]["order"] = order
