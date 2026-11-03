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


# ── Inclusions (Phase 6A) ────────────────────────────────────────────────────
# A choice's ``includes`` names choices in other categories that it applies
# with its own members, by reference: one batch, and it matches only while
# every included choice's conditions hold too. ``resolve_inclusions`` is the
# one authority; apply, projection and the batch check all read it.

def home_categories(categories):
    """``{choice id: the category that shows it}``: the first by order that lists it."""
    from . import control_preset_categories as pc

    homes = {}
    for cid in pc.ordered(categories or {}):
        for choice in categories[cid].get("choices") or []:
            homes.setdefault(choice, cid)
    return homes


def _control_keys(preset):
    """What a choice sets, for the overlap rule: settings, switches and the nodes they set."""
    keys = {("binding", bid) for bid in preset.get("members") or {}}
    keys |= {("switch", eid) for eid in preset.get("execution") or {}}
    keys |= {("node", path) for path in cm.execution_effects(preset.get("execution") or {}) or {}}
    return keys


def _targets(preset):
    return (sum(1 + ("seed_mode" in m) for m in (preset.get("members") or {}).values())
            + len(cm.execution_effects(preset.get("execution") or {}) or {}))


def _quote(definitions, identity):
    return f"“{(definitions.get(identity) or {}).get('name') or identity}”"


def inclusion_problems_of(definitions, categories, identity, *, homes=None):
    """Every structural problem with choice ``identity``'s ``includes``, in
    order, as ``{rule, other, message}``. Rules: ``missing`` (no such choice),
    ``nested`` (it includes others itself, or is this choice: one level only),
    ``category`` (it is in this choice's own category), ``shared_category``
    (two included choices share a category), ``overlap`` (two parts set one
    setting, switch or node) and ``budget`` (more than 64 targets merged).
    Whether an included choice can apply now is a state, not a problem here."""
    preset = definitions[identity]
    includes = preset.get("includes") or []
    if not includes:
        return []
    homes = home_categories(categories) if homes is None else homes
    problems, parts, by_home = [], [(identity, preset)], {}
    for other in includes:
        included = definitions.get(other)
        if included is None:
            problems.append({"rule": "missing", "other": other,
                             "message": "An included choice no longer exists. Remove it in Manage."})
            continue
        if other == identity:
            problems.append({"rule": "nested", "other": other, "message": "A choice can't include itself."})
            continue
        if included.get("includes"):
            problems.append({"rule": "nested", "other": other,
                             "message": f"{_quote(definitions, other)} includes other choices itself, so it can't be included."})
            continue
        home = homes.get(other)
        # A category problem still counts the choice's Controls below, so a
        # later capture that also overlaps (or passes the budget) is its own
        # problem, and fixing the category alone shows the next one.
        if home is not None and home == homes.get(identity):
            problems.append({"rule": "category", "other": other,
                             "message": f"{_quote(definitions, other)} is in this choice's own category, so it can't be included."})
        elif home is not None and home in by_home:
            problems.append({"rule": "shared_category", "other": other,
                             "message": f"{_quote(definitions, by_home[home])} and {_quote(definitions, other)} are in one "
                                        "category, so only one of them can be included."})
        elif home is not None:
            by_home[home] = other
        parts.append((other, included))
    owners: dict = {}
    for other, part in parts:
        clash = next((owners[key] for key in sorted(_control_keys(part)) if key in owners), None)
        if clash is not None:
            problems.append({"rule": "overlap", "other": other, "message": (
                f"{_quote(definitions, other)} sets a Control this choice sets too." if clash == identity
                else f"{_quote(definitions, clash)} and {_quote(definitions, other)} set the same Control.")})
            continue
        for key in _control_keys(part):
            owners[key] = other
    if sum(_targets(part) for _, part in parts) > cm.MAX_PRESET_TARGETS:
        problems.append({"rule": "budget", "other": None, "message": (
            "With the choices it includes, this choice holds more than 64 targets; a seed with a mode counts as two, "
            "and each node a switch sets counts as one.")})
    return problems


def resolve_inclusions(definitions, categories, identity, *, homes=None):
    """``(parts, None)``: the definitions choice ``identity`` applies, its own
    first, then each choice it includes in order; or ``(None, problem)``."""
    problems = inclusion_problems_of(definitions, categories, identity, homes=homes)
    if problems:
        return None, problems[0]
    preset = definitions[identity]
    return [(identity, preset), *((other, definitions[other]) for other in preset.get("includes") or [])], None


def inclusion_problems(definitions, categories):
    """``{(parent, rule, other): message}`` over every choice: the batch check
    compares this before and after a batch's edits."""
    homes = home_categories(categories)
    return {(identity, p["rule"], p["other"]): p["message"]
            for identity, preset in definitions.items() if preset.get("includes")
            for p in inclusion_problems_of(definitions, categories, identity, homes=homes)}


def expand_choice(view, definitions, categories, identity, *, own_only=False, homes=None):
    """``(changes, reason, own unavailable ids)`` to apply choice ``identity``:
    its own members' targets and, unless ``own_only``, every included choice's,
    as one batch. ``reason`` is non-empty when it can't apply; an included
    part's reason names that choice."""
    preset = definitions[identity]
    if own_only:
        parts = [(identity, preset)]
    else:
        parts, problem = resolve_inclusions(definitions, categories, identity, homes=homes)
        if problem:
            return {}, problem["message"], []
    changes = {}
    for part_id, part in parts:
        part_changes, unavailable = _expand(view, part)
        if unavailable:
            text = _reason(view, unavailable, part)
            if part_id == identity:
                return {}, text, unavailable
            return {}, f"Included {_quote(definitions, part_id)}: {text}", []
        changes.update(part_changes)
    return changes, "", []


def _merged(parts):
    """One definition holding every part's members and switches (no overlap)."""
    members, execution = {}, {}
    for _, part in parts:
        members.update(part.get("members") or {})
        execution.update(part.get("execution") or {})
    return {"members": members, **({"execution": execution} if execution else {})}


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
                     + " changed since this choice was captured. Update the choice in Manage.")
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
    homes = home_categories(categories)
    built, conditions, own_conditions = {}, {}, {}

    def holds(preset, cleaned):
        """Every setting and switch of ``preset`` holds its captured state now."""
        return (all(condition_holds(bindings.get(bid, {}), match_condition(
                        bindings.get(bid, {}), member, cleaned.get(f"binding:{bid}:value", _UNSET)))
                    for bid, member in preset["members"].items())
                and all(_execution_matches(switches.get(eid, {}), member)
                        for eid, member in (preset.get("execution") or {}).items()))

    # Choices that include nothing first: an including choice reads their states.
    for identity, preset in sorted(presets.items(), key=lambda item: bool(item[1].get("includes"))):
        changes, reason, unavailable = expand_choice(view, presets, categories, identity, homes=homes)
        included = [presets[other] for other in preset.get("includes") or [] if other in presets] if not reason else []
        for other in preset.get("includes") or []:
            if not reason and built.get(other, {}).get("state") in ("unavailable", "refused"):
                reason = f"Included {_quote(presets, other)}: {built[other]['reason']}"
        cleaned, rejected = (cd.validate_binding_changes(view, changes, prompt=prompt, expanded=True)
                             if not reason and changes else ({}, []))
        state = "unavailable" if reason else "refused" if rejected else "match"
        members = []
        for bid, member in preset["members"].items():
            descriptor = bindings.get(bid, {})
            status = "unavailable" if bid in unavailable else "ok"
            members.append({"binding_id": bid, "preview": _preview(member["value"], descriptor), "status": status,
                            **({"seed_mode": member["seed_mode"]} if "seed_mode" in member else {}),
                            # Two row lists can share a summary; Manage's hint needs the real comparison.
                            **({"differs": control_rows.canonical(member["value"]) != control_rows.canonical(descriptor.get("value"))}
                               if descriptor.get("kind") == control_rows.ROWS_KIND else {})})
        # Members and switches stay the choice's own (Manage's ticks and Update);
        # matching needs every included choice's conditions too.
        execution = [{"execution_id": eid, "preview": _mode_preview(member), "status": "unavailable" if eid in unavailable else "ok"}
                     for eid, member in (preset.get("execution") or {}).items()]
        if state == "match" and not all(holds(part, cleaned) for part in [preset, *included]):
            state = "differs"
        if categories:
            own_conditions[identity] = _conditions(view, preset, cleaned)
            conditions[identity] = _conditions(view, _merged([(identity, preset), *((None, p) for p in included)]), cleaned)
        built[identity] = {"id": identity, "name": preset["name"], "description": preset["description"],
                           "order": preset["order"], "origin": (origins or {}).get(identity, "workflow"),
                           "state": state, "members": members, "execution": execution, "unavailable": unavailable,
                           "reason": reason or (_refused_reason(view, rejected) if rejected else "")}
    for identity, preset in presets.items():
        if preset.get("includes"):
            built[identity]["includes"] = [{
                "id": other, "name": presets[other]["name"] if other in presets else "",
                "status": "missing" if other not in presets
                else "unavailable" if built[other]["state"] in ("unavailable", "refused") else "ok"}
                for other in preset["includes"]]
    rows = [built[identity] for identity, _ in sorted(presets.items(), key=lambda item: item[1]["order"])]
    # Overlap compares what matching uses (included conditions too); linked
    # compares each choice's own, so a deliberate inclusion never links rows.
    category_rows = (control_preset_categories.category_rows(categories, presets, conditions, labels=_control_labels(view),
                                                            origins=category_origins, dropped=set(dropped),
                                                            linked_conditions=own_conditions)
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
            raise cd.ControlValueError("A setting id in this choice is invalid.")
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
            raise cd.ControlValueError("A setting id in this choice is invalid.")
        if bid in views and bid not in bindings:
            raise cd.ControlValueError("A View can't be part of a choice.")
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
        raise cd.ControlValueError("Two switches in this choice set the same node differently.")
    if sum(1 + ("seed_mode" in entry) for entry in result.values()) + len(effects) > cm.MAX_PRESET_TARGETS:
        raise cd.ControlValueError("A choice may hold at most 64 targets; a seed with a mode counts as two, "
                                   "and each node a switch sets counts as one.")
    return result, switched


def _entry(base, members, switched):
    entry = {**base, "members": members}
    entry.pop("execution", None)
    if switched:
        entry["execution"] = switched
    return entry


def _includes_value(raw):
    """A written ``includes``: a list of at most 16 different choice ids."""
    if (not isinstance(raw, list) or len(raw) > cm.MAX_PRESET_INCLUDES
            or any(not isinstance(c, str) or not cm.ID_RE.fullmatch(c) or c in cm.FORBIDDEN_KEYS for c in raw)
            or len(set(raw)) != len(raw)):
        raise cd.ControlValueError(f"Choose at most {cm.MAX_PRESET_INCLUDES} different choices to include.")
    return list(raw)


def _with_includes(entry, includes):
    entry.pop("includes", None)
    if includes:
        entry["includes"] = list(includes)
    return entry


def validate_preset_changes(view, changes, *, prompt=None, structural=None, inventory=None, execution_inventory=None,
                            own_only=False):
    """Return canonical preset edits or an ordinary atomic value/mode batch.

    Category targets (``preset_category:<id>:<field>``) validate here too, in
    request order against the same ephemeral definitions, so one request can
    create a choice and list it. Mounted structural intents are shape-checked
    only and forwarded raw. The private definitions never travel in its
    bounded publication view. ``own_only`` applies a choice without the
    choices it includes (the Launcher's requirement simulations).
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
                raise cd.ControlValueError("This choice target is unavailable.")
            if view.get("error") or (view.get("relay") or {}).get("status") == "error":
                raise cd.ControlValueError("The Sonder Control Relay cannot be read.")
            if field == "apply" and (len(changes) != 1 or raw is not True):
                raise cd.ControlValueError("Apply one choice by itself with true.")
            if field in ("create", "capture"):
                switches = raw.get("execution", []) if isinstance(raw, dict) else None
                if not isinstance(raw, dict) or not isinstance(raw.get("members"), list) or not isinstance(switches, list):
                    raise cd.ControlValueError("Choose the Controls to capture.")
                chosen = raw["members"] + switches
                if not chosen or len(chosen) > 64 or any(not isinstance(x, str) for x in chosen):
                    raise cd.ControlValueError("Choose 1 to 64 Controls.")
            if field in ("remove", "reset") and raw is not True:
                raise cd.ControlValueError(f"Send true to {field} a choice.")
            if field == "reset" and structural != "sdk":
                raise cd.ControlValueError("Reset to the workflow's version is available in the Sonder App.")
            if field == "move" and (not isinstance(raw, dict) or (raw.get("before") is not None and not isinstance(raw["before"], str))):
                raise cd.ControlValueError("Name the choice to move before.")
            if field == "name" and isinstance(raw, str):
                raw = raw.strip()  # stored trimmed; names saved earlier are not rewritten
            if field in ("name", "description"):
                limit = cm.LABEL_MAX if field == "name" else cm.DESCRIPTION_MAX
                if not isinstance(raw, str) or len(raw) > limit or (field == "name" and cm.blank_name(raw)):
                    raise cd.ControlValueError(f"A choice {field} must be text of 1–{limit} characters." if field == "name" else f"A choice description may hold at most {limit} characters.")
            if field not in {"create", "capture", "apply", "name", "description", "move", "remove", "reset", "includes"}:
                raise cd.ControlValueError("This choice target is unavailable.")
            if field == "includes":
                raw = _includes_value(raw)
            if field == "create" and "includes" in raw:
                _includes_value(raw["includes"])
            if structural == "relay":
                cleaned[target] = copy.deepcopy(raw)
                continue
            existing = definitions.get(identity)
            if field == "apply":
                if not existing:
                    raise cd.ControlValueError("This choice no longer exists. Open Manage.")
                # Its own members and every included choice's: one batch.
                expanded, reason, _ = expand_choice(view, definitions, categories, identity, own_only=own_only)
                if reason:
                    raise cd.ControlValueError(reason)
                # Values and modes validate and land as one batch: one native
                # history step on the canvas, one sidecar mutation in the app.
                values, errors = cd.validate_binding_changes(view, expanded, prompt=prompt, structural=structural,
                                                             expanded=True)
                if errors:
                    raise cd.ControlValueError(_refused_reason(view, errors))
                return values, []
            if field == "create":
                if existing:
                    raise cd.ControlValueError("This choice id is already in use.")
                if len(definitions) >= cm.MAX_PRESETS:
                    raise cd.ControlValueError("At most 32 choices fit in one workflow.")
                name = raw.get("name")
                entry = _entry({"name": name.strip() if isinstance(name, str) else name, "description": raw.get("description", ""),
                                "order": min(cm.ORDER_LIMIT, max((p["order"] for p in definitions.values()), default=-1) + 1)},
                               *_capture_preset(view, raw["members"], execution=raw.get("execution", [])))
                _with_includes(entry, raw.get("includes"))
                if identity not in cm.read_presets({identity: entry}):
                    raise cd.ControlValueError("A choice needs a name of 1–120 characters and a description of at most 1000 characters.")
                definitions[identity] = entry
                cleaned[target] = entry
                continue
            if not existing and field not in ("remove", "reset"):
                raise cd.ControlValueError("This choice no longer exists. Open Manage.")
            if field == "capture":
                entry = _entry(existing, *_capture_preset(view, raw["members"], existing, raw.get("execution", [])))
                definitions[identity] = entry
                cleaned[target] = entry
            elif field == "includes":
                _with_includes(definitions[identity], raw)
                cleaned[target] = raw
            elif field in ("name", "description"):
                definitions[identity][field] = raw
                # Every writer stores what this accepts; refuse what its reader would drop.
                if identity not in cm.read_presets({identity: definitions[identity]}):
                    raise cd.ControlValueError("A choice needs a name of 1–120 characters and a description of at most 1000 characters.")
                cleaned[target] = raw
            elif field == "move":
                before = raw.get("before")
                if before == identity or (before is not None and before not in definitions):
                    raise cd.ControlValueError("Choose another choice to move before.")
                cleaned[target] = {"before": before}
            else:
                cleaned[target] = True
                definitions.pop(identity, None)
                if field == "remove":
                    # A deleted choice leaves its categories in the same edit.
                    pc.drop_choice(categories, identity)
        except (cd.ControlValueError, TypeError, ValueError) as exc:
            rejected.append({"target": target, "reason": str(exc)})
    if not rejected and structural != "relay" and any(s in cd.PRESET_SOURCES for s in sources.values()):
        rejected.extend(_inclusion_refusals(view, changes, sources, definitions, categories, start))
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


def _inclusion_refusals(view, changes, sources, definitions, categories, start):
    """Inclusion problems this batch makes (``inclusion_problems`` before and
    after): one already there (a hand edit, an older pack) never blocks an
    unrelated edit. A ``reset`` can't be checked (what it restores isn't
    visible here): problems naming a reset choice, and the category rules
    after a category reset, are left to show at read; the rest of the batch
    is still checked."""
    parsed = {target: cd.parse_target(target) for target in changes}
    reset = {p[1] for p in parsed.values() if p and p[0] == "preset" and p[2] == "reset"}
    category_reset = any(p and p[0] == "preset_category" and p[2] == "reset" for p in parsed.values())
    before = inclusion_problems(view.get("_presets", {}), start)
    added = {(parent, rule, other): message for (parent, rule, other), message in inclusion_problems(definitions, categories).items()
             if (parent, rule, other) not in before and parent not in reset and other not in reset
             and not (category_reset and rule in ("category", "shared_category"))}
    if not added:
        return []
    removed = {p[1] for p in parsed.values() if p and p[0] == "preset" and p[2] == "remove"}
    refusals, seen = [], set()
    # Deleting an included choice names the choices that include it.
    for other in removed:
        parents = [parent for (parent, rule, gone) in added if rule == "missing" and gone == other]
        if parents:
            names = " and ".join(_quote(definitions, parent) for parent in sorted(parents, key=lambda p: definitions[p]["order"]))
            verb = "includes" if len(parents) == 1 else "include"
            refusals.append({"target": f"preset:{other}:remove", "reason": f"{names} {verb} this choice. Take it out there first."})
            seen.update((parent, "missing", other) for parent in parents)
    preset_targets = [t for t in changes if sources[t] in cd.PRESET_SOURCES]
    for (parent, rule, other), message in added.items():
        if (parent, rule, other) in seen:
            continue
        # The target that names the choice, else the batch's first.
        target = next((t for t in preset_targets if parsed[t][0] == "preset" and parsed[t][1] in (parent, other)),
                      preset_targets[0] if preset_targets else next(iter(changes)))
        label = (definitions.get(parent) or {}).get("name") or parent
        refusals.append({"target": target, "reason": f"“{label}”: {message}"})
    return refusals


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
    elif field == "includes":
        _with_includes(definitions[identity], value)
    elif field == "move":
        ordered = sorted(definitions, key=lambda key: definitions[key]["order"])
        ordered.remove(identity)
        before = value.get("before")
        ordered.insert(ordered.index(before) if before else len(ordered), identity)
        for order, key in enumerate(ordered):
            definitions[key]["order"] = order
