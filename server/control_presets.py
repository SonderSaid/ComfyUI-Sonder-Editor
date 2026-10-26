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


def _switches(view):
    return {d["execution_id"]: d for d in view.get("execution", []) or []}


def _drifted(descriptor, member):
    """The switch no longer sets what the preset captured: other group members,
    or its anchor now on another node. Applying would guess, so it is refused
    until the preset is updated."""
    members = descriptor.get("members") or []
    if "modes" in member:
        return {m["path"] for m in members} != set(member["modes"])
    return (len(members) != 1 or members[0]["path"] != member["node"]["path"]
            or members[0].get("class_type") != member["node"]["class_type"])


def _execution_unavailable(view, descriptor, member):
    # A hidden switch's modes never run in the app, like a hidden setting's value.
    return (descriptor.get("status") != "ok" or descriptor.get("read_only") or descriptor.get("hidden")
            or bool(view.get("execution_error"))
            or ("modes" in member) != (descriptor.get("kind") == "group") or _drifted(descriptor, member))


def _expand(view, preset):
    bindings = {d["binding_id"]: d for d in view.get("bindings", [])}
    switches = _switches(view)
    changes, unavailable = {}, []
    for identity, member in preset["members"].items():
        descriptor = bindings.get(identity, {})
        if descriptor.get("status") != "ok" or descriptor.get("hidden") or view.get("error"):
            unavailable.append(identity)
            continue
        changes[f"binding:{identity}:value"] = member["value"]
        if descriptor.get("kind") == "SEED" and descriptor.get("seed_mode") is not None and "seed_mode" in member:
            changes[f"binding:{identity}:seed_mode"] = member["seed_mode"]
    for identity, member in (preset.get("execution") or {}).items():
        descriptor = switches.get(identity, {})
        if view.get("error") or _execution_unavailable(view, descriptor, member):
            unavailable.append(identity)
            continue
        if "modes" in member:
            changes[f"execution:{identity}:modes"] = dict(member["modes"])
        else:
            changes[f"execution:{identity}:mode"] = member["mode"]
    return changes, unavailable


def _labels(view):
    labels = {d["binding_id"]: d.get("label") or d["binding_id"] for d in view.get("bindings", [])}
    labels.update({d["execution_id"]: d.get("label") or d["execution_id"] for d in view.get("execution", []) or []})
    return labels


def _reason(view, identities, preset=None):
    """Why a preset can't apply. A switch whose nodes changed since capture is
    named apart: it needs Update, not a repair."""
    labels = _labels(view)
    switches = _switches(view)
    members = (preset or {}).get("execution") or {}
    drifted = [i for i in identities if i in members and switches.get(i, {}).get("status") == "ok"
               and not switches[i].get("read_only") and _drifted(switches[i], members[i])]
    others = [i for i in identities if i not in drifted]
    parts = []
    if others:
        parts.append("Unavailable Controls: " + ", ".join(labels.get(i, i) for i in others) + ". Repair them in Manage.")
    if drifted:
        parts.append(", ".join(f"“{labels.get(i, i)}”" for i in drifted)
                     + " changed since this preset was captured. Update the preset in Manage.")
    return " ".join(parts)


def _refused_reason(view, rejected):
    labels = _labels(view)
    return " ".join(f"{labels.get((cd.parse_target(r['target']) or ('', '', ''))[1], r['target'])}: {r['reason']}" for r in rejected)


def _mode_preview(member):
    if "modes" in member:
        modes = list(member["modes"].values())
        off = sum(1 for mode in modes if mode != "enabled")
        if len(set(modes)) == 1:
            return modes[0].capitalize()
        return f"Mixed ({off} of {len(modes)} off)"
    return member["mode"].capitalize()


def _execution_matches(descriptor, member):
    live = {m["path"]: m["mode"] for m in descriptor.get("members") or []}
    wanted = member["modes"] if "modes" in member else {member["node"]["path"]: member["mode"]}
    return live == wanted


def _preview(value, descriptor):
    """A member's value as text; a row list reads as its summary (``3 LoRAs · 2 on``)."""
    if isinstance(value, list):
        return control_rows.summary(value, descriptor if descriptor.get("kind") == control_rows.ROWS_KIND else {})[:200]
    return ("true" if value is True else "false" if value is False else "null" if value is None else str(value))[:200]


# A value the batch did not validate: its condition can never hold.
_UNSET = object()


def match_condition(descriptor, member, value):
    """What a setting member requires to match on this host: its coerced
    ``value``, plus ``seed_mode`` for a captured seed mode. A fixed seed needs
    both; an advancing seed needs its mode only (its number moves every run);
    a stored mode is ignored where the host binding has no mode widget. The
    one rule ``preset_states`` matches by and the category warnings compare."""
    mode = member.get("seed_mode") if descriptor.get("seed_mode") is not None else None
    condition = {}
    if mode is not None:
        condition["seed_mode"] = mode
    if mode in (None, "fixed"):
        condition["value"] = value
    return condition


def condition_holds(descriptor, condition):
    if "seed_mode" in condition and condition["seed_mode"] != descriptor.get("seed_mode"):
        return False
    if "value" in condition:
        if condition["value"] is _UNSET:
            return False
        try:
            return cd.coerce_binding_value(descriptor, descriptor.get("value")) == condition["value"]
        except cd.ControlValueError:
            return False
    return True


def _stored_value(descriptor, raw):
    """A member's value for comparison when its preset did not validate as a
    batch: coerced like the batch would be, else its stored form (a decimal
    string of an INT/SEED as the number)."""
    if descriptor.get("status") == "ok":
        try:
            return cd.coerce_binding_value(descriptor, raw)
        except (cd.ControlValueError, TypeError, ValueError, OverflowError):
            pass
    if isinstance(raw, str) and descriptor.get("kind") in (None, "INT", "SEED"):
        try:
            return cd.coerce_control_value("INT", raw)
        except (cd.ControlValueError, TypeError, ValueError, OverflowError):
            pass
    return raw


def _conditions(view, preset, cleaned):
    """``{control key: condition}`` for one preset: ``("binding", id)`` per
    setting and ``("node", path)`` per node its switches set. Values a batch
    validated are the ones matching uses."""
    bindings = {d["binding_id"]: d for d in view.get("bindings", [])}
    result = {}
    for bid, member in preset["members"].items():
        descriptor = bindings.get(bid, {})
        key = f"binding:{bid}:value"
        value = cleaned[key] if key in cleaned else _stored_value(descriptor, member["value"])
        if descriptor.get("status") == "ok":
            condition = match_condition(descriptor, member, value)
        else:
            # No host binding to read: the member's own capture decides.
            condition = {"value": value, **({"seed_mode": member["seed_mode"]} if "seed_mode" in member else {})}
            if member.get("seed_mode") not in (None, "fixed"):
                condition.pop("value")
        result[("binding", bid)] = condition
    for path, mode in (cm.execution_effects(preset.get("execution") or {}) or {}).items():
        result[("node", path)] = {"mode": mode}
    return result


def _control_labels(view):
    labels = {("binding", bid): label for bid, label in _labels(view).items()}
    for descriptor in view.get("execution", []) or []:
        for member in descriptor.get("members") or []:
            labels.setdefault(("node", member.get("path")), descriptor.get("label") or descriptor.get("execution_id"))
    return labels


def preset_projection(view, presets, categories=None, *, origins=None, category_origins=None, prompt=None, dropped=()):
    """The one pure match authority, called after each host's effective
    projection: ``(preset rows, category rows)``. Category warnings are
    computed only when the workflow has categories."""
    from . import control_preset_categories

    bindings = {d["binding_id"]: d for d in view.get("bindings", [])}
    switches = _switches(view)
    rows, conditions = [], {}
    for identity, preset in sorted(presets.items(), key=lambda item: item[1]["order"]):
        changes, unavailable = _expand(view, preset)
        cleaned, rejected = (cd.validate_binding_changes(view, changes, prompt=prompt, expanded=True)
                             if not unavailable and changes else ({}, []))
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
            if state == "match" and not condition_holds(
                    descriptor, match_condition(descriptor, member, cleaned.get(f"binding:{bid}:value", _UNSET))):
                state = "differs"
        execution = []
        for eid, member in (preset.get("execution") or {}).items():
            status = "unavailable" if eid in unavailable else "ok"
            execution.append({"execution_id": eid, "preview": _mode_preview(member), "status": status})
            if state == "match" and not _execution_matches(switches.get(eid, {}), member):
                state = "differs"
        if categories:
            conditions[identity] = _conditions(view, preset, cleaned)
        rows.append({"id": identity, "name": preset["name"], "description": preset["description"],
                     "order": preset["order"], "origin": (origins or {}).get(identity, "workflow"),
                     "state": state, "members": members, "execution": execution, "unavailable": unavailable,
                     "reason": _reason(view, unavailable, preset) if unavailable else _refused_reason(view, rejected) if rejected else ""})
    category_rows = (control_preset_categories.category_rows(categories, presets, conditions, labels=_control_labels(view),
                                                            origins=category_origins, dropped=set(dropped))
                     if categories else [])
    return rows, category_rows


def preset_states(view, presets, *, origins=None, prompt=None):
    """The preset rows alone (``preset_projection`` without categories)."""
    return preset_projection(view, presets, origins=origins, prompt=prompt)[0]


def _capture_execution(view, identities, previous=None):
    """Each switch's current modes: a node or instance with its identity, a
    group with its exact members (Mixed included)."""
    switches = _switches(view)
    result = {}
    for eid in identities:
        if not isinstance(eid, str) or not cm.ID_RE.fullmatch(eid) or eid in cm.FORBIDDEN_KEYS:
            raise cd.ControlValueError("A preset member id is invalid.")
        descriptor = switches.get(eid, {})
        if descriptor.get("status") != "ok" or descriptor.get("read_only") or descriptor.get("hidden"):
            if previous and eid in (previous.get("execution") or {}):
                result[eid] = copy.deepcopy(previous["execution"][eid])
                continue
            raise cd.ControlValueError(_reason(view, [eid]))
        members = descriptor["members"]
        if descriptor["kind"] == "group":
            result[eid] = {"modes": {m["path"]: m["mode"] for m in members}}
        else:
            result[eid] = {"mode": members[0]["mode"], "node": {"path": members[0]["path"], "class_type": members[0]["class_type"]}}
    return result


def _capture(view, members, previous=None):
    """The settings' current values; ``_capture_preset`` adds node switches."""
    return _capture_preset(view, members, previous)[0]


def _capture_preset(view, members, previous=None, execution=()):
    if (not isinstance(members, list) or not isinstance(execution, (list, tuple)) or not (members or execution)
            or len(set(members)) != len(members) or len(set(execution)) != len(execution)):
        raise cd.ControlValueError("Choose at least one Control, without duplicates.")
    bindings = {d["binding_id"]: d for d in view.get("bindings", [])}
    # A View is inspection only: never a preset member (control_views).
    views = {d.get("view_id") for d in view.get("views", []) or []}
    result = {}
    for bid in members:
        if not isinstance(bid, str) or not cm.ID_RE.fullmatch(bid) or bid in cm.FORBIDDEN_KEYS:
            raise cd.ControlValueError("A preset member id is invalid.")
        if bid in views and bid not in bindings:
            raise cd.ControlValueError("A View can't be part of a preset.")
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
    switched = _capture_execution(view, execution, previous)
    effects = cm.execution_effects(switched)
    if effects is None:
        raise cd.ControlValueError("Two switches in this preset set the same node differently.")
    if sum(1 + ("seed_mode" in entry) for entry in result.values()) + len(effects) > cm.MAX_PRESET_TARGETS:
        raise cd.ControlValueError("A preset may hold at most 64 targets; a seed with a mode counts as two, "
                                   "and each node a switch sets counts as one.")
    return result, switched


def _entry(base, members, switched):
    entry = {**base, "members": members}
    entry.pop("execution", None)
    if switched:
        entry["execution"] = switched
    return entry


def validate_preset_changes(view, changes, *, prompt=None, structural=None, inventory=None, execution_inventory=None):
    """Return canonical preset edits or an ordinary atomic value/mode batch.

    Category targets (``preset_category:<id>:<field>``) validate here too, in
    request order against the same ephemeral definitions, so one request can
    create a choice and list it. Mounted structural intents are shape-checked
    only and forwarded raw. The private definitions never travel in its
    bounded publication view.
    """
    from . import control_preset_categories as pc

    definitions = copy.deepcopy(view.get("_presets", {}))
    categories = copy.deepcopy(view.get("_preset_categories", {}))
    start = copy.deepcopy(categories)
    sources = {target: (cd.parse_target(target) or (None,))[0] for target in changes}
    ordinary = {target: raw for target, raw in changes.items() if sources[target] not in cd.PRESET_SOURCES}
    cleaned, rejected = cd.validate_binding_changes(view, ordinary, prompt=prompt, structural=structural, inventory=inventory,
                                                    execution_inventory=execution_inventory) if ordinary else ({}, [])
    for target, raw in changes.items():
        parsed = cd.parse_target(target)
        if not parsed or parsed[0] not in cd.PRESET_SOURCES:
            continue
        if parsed[0] == "preset_category":
            # A mixed batch with ``preset:<id>:apply`` is refused by the apply target.
            try:
                cleaned[target] = _category_change(view, categories, definitions, parsed[1], parsed[2], raw, structural)
            except (cd.ControlValueError, TypeError, ValueError) as exc:
                rejected.append({"target": target, "reason": str(exc)})
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
                switches = raw.get("execution", []) if isinstance(raw, dict) else None
                if not isinstance(raw, dict) or not isinstance(raw.get("members"), list) or not isinstance(switches, list):
                    raise cd.ControlValueError("Choose the Controls to capture.")
                chosen = raw["members"] + switches
                if not chosen or len(chosen) > 64 or any(not isinstance(x, str) for x in chosen):
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
                if not isinstance(raw, str) or len(raw) > limit or (field == "name" and cm.blank_name(raw)):
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
                    raise cd.ControlValueError(_reason(view, unavailable, existing))
                # Values and modes validate and land as one batch: one native
                # history step on the canvas, one sidecar mutation in the app.
                values, errors = cd.validate_binding_changes(view, expanded, prompt=prompt, structural=structural,
                                                             expanded=True)
                if errors:
                    raise cd.ControlValueError(_refused_reason(view, errors))
                return values, []
            if field == "create":
                if existing:
                    raise cd.ControlValueError("This preset id is already in use.")
                if len(definitions) >= cm.MAX_PRESETS:
                    raise cd.ControlValueError("At most 32 presets fit in one workflow.")
                name = raw.get("name")
                entry = _entry({"name": name.strip() if isinstance(name, str) else name, "description": raw.get("description", ""),
                                "order": min(cm.ORDER_LIMIT, max((p["order"] for p in definitions.values()), default=-1) + 1)},
                               *_capture_preset(view, raw["members"], execution=raw.get("execution", [])))
                if identity not in cm.read_presets({identity: entry}):
                    raise cd.ControlValueError("A preset needs a name of 1–120 characters and a description of at most 1000 characters.")
                definitions[identity] = entry
                cleaned[target] = entry
                continue
            if not existing and field not in ("remove", "reset"):
                raise cd.ControlValueError("This preset no longer exists. Open Manage.")
            if field == "capture":
                entry = _entry(existing, *_capture_preset(view, raw["members"], existing, raw.get("execution", [])))
                definitions[identity] = entry
                cleaned[target] = entry
            elif field in ("name", "description"):
                definitions[identity][field] = raw
                # Every writer stores what this accepts; refuse what its reader would drop.
                if identity not in cm.read_presets({identity: definitions[identity]}):
                    raise cd.ControlValueError("A preset needs a name of 1–120 characters and a description of at most 1000 characters.")
                cleaned[target] = raw
            elif field == "move":
                before = raw.get("before")
                if before == identity or (before is not None and before not in definitions):
                    raise cd.ControlValueError("Choose another preset to move before.")
                cleaned[target] = {"before": before}
            else:
                cleaned[target] = True
                definitions.pop(identity, None)
                if field == "remove":
                    # A deleted choice leaves its categories in the same edit.
                    pc.drop_choice(categories, identity)
        except (cd.ControlValueError, TypeError, ValueError) as exc:
            rejected.append({"target": target, "reason": str(exc)})
    if not rejected and structural != "relay" and "preset_category" in sources.values():
        # Only a duplicate this batch makes is refused: one already there (a
        # Replace, a reset, a hand edit) is resolved by the first category and
        # warned in Manage, and never blocks an unrelated edit.
        before = pc.claims(start)
        for choice, cids in pc.introduced_duplicates(start, categories).items():
            names = " and ".join(f"“{categories[cid]['name']}”" for cid in pc.ordered({c: categories[c] for c in cids}))
            label = definitions.get(choice, {}).get("name", choice)
            reason = f"“{label}” would be in two categories, {names}. Take it out of one."
            # Each target that put it into a category it was not in before.
            added = cids - before.get(choice, set())
            for target in changes:
                parsed = cd.parse_target(target)
                if sources[target] == "preset_category" and parsed[1] in added and parsed[2] in ("create", "choices"):
                    rejected.append({"target": target, "reason": reason})
    return ({} if rejected else cleaned), rejected


def _category_change(view, categories, definitions, identity, field, raw, structural):
    """One ``preset_category:<id>:<field>`` target, applied to this batch's
    ephemeral ``categories``; the canonical value its writers store."""
    from . import control_preset_categories as pc

    if identity in cm.FORBIDDEN_KEYS or structural not in ("canvas", "sdk", "relay") or field not in pc.FIELDS:
        raise cd.ControlValueError("This category target is unavailable.")
    if view.get("error") or (view.get("relay") or {}).get("status") == "error":
        raise cd.ControlValueError("The Sonder Control Relay cannot be read.")
    if field == "create":
        if not isinstance(raw, dict) or set(raw) - {"name", "description", "choices"}:
            raise cd.ControlValueError("Name the new category.")
        name, description, choices = raw.get("name"), raw.get("description", ""), raw.get("choices", [])
    else:
        name = raw if field == "name" else None
        description = raw if field == "description" else ""
        choices = raw if field == "choices" else []
    if field in ("create", "name"):
        if not isinstance(name, str) or cm.blank_name(name) or len(name.strip()) > cm.LABEL_MAX:
            raise cd.ControlValueError(f"A category name must be text of 1–{cm.LABEL_MAX} characters.")
        name = name.strip()
    if not isinstance(description, str) or len(description) > cm.CATEGORY_DESCRIPTION_MAX:
        raise cd.ControlValueError(f"A category description may hold at most {cm.CATEGORY_DESCRIPTION_MAX} characters.")
    if (not isinstance(choices, list) or len(choices) > cm.MAX_PRESETS
            or any(not isinstance(c, str) or not cm.ID_RE.fullmatch(c) or c in cm.FORBIDDEN_KEYS for c in choices)
            or len(set(choices)) != len(choices)):
        raise cd.ControlValueError(f"Choose at most {cm.MAX_PRESETS} different choices.")
    if field == "move" and (not isinstance(raw, dict) or set(raw) != {"before"}
                            or (raw["before"] is not None and not isinstance(raw["before"], str))):
        raise cd.ControlValueError("Choose a category to move before.")
    if field in ("remove", "reset") and raw is not True:
        raise cd.ControlValueError(f"Send true to {field} a category.")
    if field == "reset" and structural != "sdk":
        raise cd.ControlValueError("Reset to the workflow's version is available in the Sonder App.")
    value = ({"name": name, "description": description, "choices": list(choices)} if field == "create"
             else name if field == "name" else copy.deepcopy(raw))
    if structural == "relay":
        # The mounted pre-check: shape only; the canvas writer revalidates.
        return value
    existing = categories.get(identity)
    if field == "create":
        if existing:
            raise cd.ControlValueError("This category id is already in use.")
        if len(categories) >= cm.MAX_PRESET_CATEGORIES:
            raise cd.ControlValueError(f"At most {cm.MAX_PRESET_CATEGORIES} categories fit in one workflow.")
        value["order"] = min(cm.ORDER_LIMIT, max((c["order"] for c in categories.values()), default=-1) + 1)
    elif field not in ("remove", "reset") and not existing:
        raise cd.ControlValueError("This category no longer exists. Open Manage.")
    if field in ("create", "choices"):
        unknown = [c for c in choices if c not in definitions]
        if unknown:
            raise cd.ControlValueError("A choice in this list no longer exists. Refresh Controls.")
    if field == "move" and (raw["before"] == identity or (raw["before"] is not None and raw["before"] not in categories)):
        raise cd.ControlValueError("Choose another category to move before.")
    pc.edit_categories(categories, identity, field, value)
    if field not in ("remove", "reset") and identity not in cm.read_preset_categories({identity: categories[identity]}):
        raise cd.ControlValueError("This category would be unreadable. Check its name and description.")
    return value


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
