"""Preset categories (Controls Modes): grouped, exclusive choices.

A category is workflow presentation in the Relay manifest's
``preset_categories`` section (``control_manifest.read_preset_categories``):
``{<cid>: {name, description, order, choices: [<preset id>, …]}}``. It owns
membership and choice order, so moving or ungrouping a choice never rewrites
the choice. Selection is never stored: the panel derives it from
``preset_states``. This module owns the category edits, the Sonder App
overlay and the authoring warnings, from match conditions that
``control_presets`` (the match authority) computes. Pure.
"""
from __future__ import annotations

import copy
from typing import Any

from . import control_manifest as cm
from . import control_rows

FIELDS = frozenset({"create", "name", "description", "choices", "move", "remove", "reset"})
# Warnings stay readable and bounded: at most this many pairs or Controls are named.
WARNING_NAMES = 4


def ordered(categories: dict) -> list:
    """Category ids by ``order``, then their position in the map (JS key order
    for a read manifest; the effective order for the Sonder App)."""
    position = {cid: index for index, cid in enumerate(categories)}
    return sorted(categories, key=lambda cid: (categories[cid]["order"], position[cid]))


def edit_categories(categories: dict, cid: str, field: str, value: Any) -> None:
    """Apply one validated edit to an ephemeral copy its writer owns.
    ``web/js/control_bindings.js`` ``applyCategoryEdit`` mirrors it."""
    if field == "create":
        categories[cid] = copy.deepcopy(value)
    elif field in ("remove", "reset"):
        categories.pop(cid, None)
    elif field in ("name", "description", "choices"):
        categories[cid][field] = copy.deepcopy(value)
    elif field == "move":
        order = ordered(categories)
        order.remove(cid)
        before = value.get("before")
        order.insert(order.index(before) if before else len(order), cid)
        for index, key in enumerate(order):
            categories[key]["order"] = index
    else:
        raise ValueError(field)


def drop_choice(categories: Any, identity: str) -> set:
    """Remove a deleted choice from every category that lists it; the ids of
    the categories changed. ``dropChoiceFromCategories`` mirrors it."""
    changed = set()
    for cid, entry in (categories.items() if isinstance(categories, dict) else []):
        if isinstance(entry, dict) and isinstance(entry.get("choices"), list) and identity in entry["choices"]:
            entry["choices"] = [choice for choice in entry["choices"] if choice != identity]
            changed.add(cid)
    return changed


def claims(categories: dict) -> dict:
    """``{choice id: {category ids listing it}}``."""
    result: dict = {}
    for cid, entry in categories.items():
        for choice in entry.get("choices") or []:
            result.setdefault(choice, set()).add(cid)
    return result


def introduced_duplicates(before: dict, after: dict) -> dict:
    """Choices a batch put into a second category: ``{choice: {cids}}``. A
    duplicate already present (Replace, reset, a hand edit) is not refused
    here; the projection resolves it and Manage warns."""
    start, end = claims(before), claims(after)
    return {choice: cids for choice, cids in end.items() if len(cids) > 1 and cids - start.get(choice, set())}


def effective_categories(authored: Any, overlays: Any) -> tuple[dict, dict]:
    """The Sonder App's categories: the workflow's, overlaid copy-on-write by
    the sidecar (a definition replaces or adds, ``{removed: true}`` hides)."""
    result = copy.deepcopy(cm.read_preset_categories(authored))
    origins = {cid: "workflow" for cid in result}
    for cid, entry in (overlays.items() if isinstance(overlays, dict) else []):
        if isinstance(entry, dict) and entry.get("removed") is True:
            result.pop(cid, None)
            origins.pop(cid, None)
            continue
        readable = cm.read_preset_categories({cid: entry})
        if cid in readable:
            result[cid] = copy.deepcopy(readable[cid])
            origins[cid] = "app_copy" if cid in (authored or {}) else "app"
    return result, origins


# ── Warnings ─────────────────────────────────────────────────────────────────

def _normal(condition: dict) -> dict:
    """A condition in comparison form: a row list as its canonical text."""
    return {key: control_rows.canonical(value) if isinstance(value, list) else value for key, value in condition.items()}


def contradicts(a: dict, b: dict) -> bool:
    """Two normalized conditions on one Control that cannot both hold."""
    return any(key in b and a[key] != b[key] for key in a)


def _conflicts(a: dict, b: dict):
    """Shared Control keys whose conditions contradict, lazily."""
    return (key for key in a if key in b and contradicts(a[key], b[key]))


def category_rows(categories: dict, presets: dict, conditions: dict, *, labels: dict,
                  origins: dict | None = None, dropped: Any = ()) -> list:
    """The categories a view shows, in order, each with its claimed choices and
    authoring warnings. A choice listed by two categories belongs to the first.
    ``conditions`` is ``{preset id: {control key: condition}}``; ``dropped``
    names choices the Sonder App removed itself, left out without a warning."""
    rows, claimed_by = [], {}
    name = lambda pid: presets[pid]["name"]
    # Normalized once: the pair loops below compare every choice with every other.
    conditions = {pid: {key: _normal(condition) for key, condition in by_key.items()} for pid, by_key in conditions.items()}
    for cid in ordered(categories):
        entry = categories[cid]
        choices, warnings, missing, repeated = [], [], [], set()
        for pid in entry.get("choices") or []:
            if pid in dropped and pid not in presets:
                continue
            if pid not in presets:
                if pid not in missing:
                    missing.append(pid)
            elif pid in repeated:
                continue  # one duplicate warning per choice
            elif pid in choices:
                repeated.add(pid)
                warnings.append({"code": "duplicate", "choice": pid,
                                 "message": f"“{name(pid)}” is listed twice; it shows once."})
            elif pid in claimed_by:
                repeated.add(pid)
                other = categories[claimed_by[pid]]["name"]
                warnings.append({"code": "duplicate", "choice": pid, "other": claimed_by[pid],
                                 "message": f"“{name(pid)}” is also in “{other}”, where it shows."})
            else:
                choices.append(pid)
                claimed_by[pid] = cid
        if missing:
            count = len(missing)
            warnings.append({"code": "missing", "choices": missing,
                             "message": f"{count} listed choice{'s' if count != 1 else ''} no longer exist{'s' if count == 1 else ''}."})
        if len(choices) < 2:
            warnings.append({"code": "too_few", "message": "Add at least two choices."})
        pairs = [(a, b) for i, a in enumerate(choices) for b in choices[i + 1:]
                 if next(_conflicts(conditions.get(a, {}), conditions.get(b, {})), None) is None]
        if pairs:
            shown = pairs[:WARNING_NAMES]
            text = "; ".join(f"“{name(a)}” and “{name(b)}”" for a, b in shown)
            more = len(pairs) - len(shown)
            warnings.append({"code": "overlap", "pairs": [list(pair) for pair in shown], "count": len(pairs),
                             "message": f"{text} can both match. Give them a shared setting with different values."
                                        + (f" And {more} more pair{'s' if more != 1 else ''}." if more else "")})
        rows.append({"id": cid, "name": entry["name"], "description": entry.get("description", ""),
                     "order": entry["order"], "origin": (origins or {}).get(cid, "workflow"),
                     "choices": choices, "warnings": warnings})
    # Linked categories: a choice here and one there set a shared Control
    # differently. One warning per row, however many rows it is linked to.
    links: dict = {row["id"]: [] for row in rows}
    for i, row in enumerate(rows):
        for other in rows[i + 1:]:
            keys = _shared_conflicts(row["choices"], other["choices"], conditions)
            if keys:
                links[row["id"]].append((other, keys))
                links[other["id"]].append((row, keys))
    for row in rows:
        linked = links[row["id"]]
        if not linked:
            continue
        if len(linked) == 1:
            there, keys = linked[0]
            names = [labels.get(key, key[1]) for key in keys[:WARNING_NAMES]]
            shared = ", ".join(names) + (" and more" if len(keys) > len(names) else "")
            message = (f"Shares {shared} with {there['name']}: a choice here can change {there['name']}, "
                       f"and {there['name']} can change this row.")
        else:
            named = [there["name"] for there, _ in linked[:WARNING_NAMES]]
            more = len(linked) - len(named)
            message = (f"Shares settings with {', '.join(named)}{f' and {more} more' if more else ''}: a choice here "
                       f"can change those rows, and they can change this one.")
        row["warnings"].append({"code": "linked", "count": len(linked), "message": message, "categories": [
            {"id": there["id"], "controls": [{"kind": key[0], "id": key[1]} for key in keys[:WARNING_NAMES]]}
            for there, keys in linked[:WARNING_NAMES]]})
    return rows


def _shared_conflicts(here: list, there: list, conditions: dict) -> list:
    """Control keys a choice in one list and a choice in the other set
    differently. Only the first few are named, so the scan stops past them."""
    keys: list = []
    for a in here:
        for b in there:
            for key in _conflicts(conditions.get(a, {}), conditions.get(b, {})):
                if key not in keys:
                    keys.append(key)
                if len(keys) > WARNING_NAMES:
                    return keys
    return keys
