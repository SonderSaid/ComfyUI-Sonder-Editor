"""What a Controls edit expected to find: the per-target check that lets an
unrelated save (an Editor FPS change, another Control) land between a gesture
and its write without refusing it, while a change to the same setting, its
inputs, or a clicked Mode's effect still refuses.

The panel sends, beside ``changes``, ``expect: {<target>: entry}`` built from
the Controls view the row was drawn from:

- ``binding:<id>:value`` / ``:seed_mode``: ``{prior, targets: [[path, input]]}``;
- ``execution:<id>:mode``: ``{prior, members: [path]}``;
- ``preset:<id>:apply``: ``{token}``, the clicked row's ``token``.

``clean`` shape-checks at every edge (the mounted relay stores it unchecked
against any view); ``check`` runs where the write is decided, against the same
fresh view the batch validated against: the canvas describe route and the
Sonder App's change under its template lock. Pure; no persistence authority.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from . import control_descriptors as cd
from . import control_manifest as cm
from . import control_rows

MAX_EXPECT_BYTES = 64 * 1024
MAX_PATHS = 64
TOKEN_CHARS = 16


def choice_token(changes: dict, view: dict | None = None) -> str:
    """A clicked Mode's effect: every target and value it would write, its
    included choices' and its groups' current nodes included, and the inputs
    each of its settings writes now (a rebind moves them)."""
    bindings = {d.get("binding_id"): d for d in (view or {}).get("bindings", [])}
    inputs = {}
    for target in changes:
        parsed = cd.parse_target(target)
        if parsed and parsed[0] == "binding":
            inputs[parsed[1]] = _pairs((bindings.get(parsed[1]) or {}).get("targets"))
    text = json.dumps({"changes": changes, "inputs": inputs}, sort_keys=True, ensure_ascii=False,
                      default=str, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:TOKEN_CHARS]


def _paths(raw: Any, *, pairs: bool) -> list:
    if not isinstance(raw, list) or len(raw) > MAX_PATHS:
        raise cd.ControlValueError("Invalid expected targets.")
    result = []
    for item in raw:
        if pairs:
            if (not isinstance(item, list) or len(item) != 2
                    or not all(isinstance(part, str) and 0 < len(part) <= 256 for part in item)):
                raise cd.ControlValueError("Invalid expected targets.")
            result.append([item[0], item[1]])
        else:
            if not isinstance(item, str) or not 0 < len(item) <= 256:
                raise cd.ControlValueError("Invalid expected targets.")
            result.append(item)
    return result


def clean(raw: Any, changes: Any) -> dict:
    """``{target: entry}`` for targets of ``changes`` only. Raises
    ``ControlValueError`` on any malformed entry; ``None`` or ``{}`` is no
    expectation (older pages, presentation edits)."""
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not isinstance(changes, dict):
        raise cd.ControlValueError("Invalid expected state.")
    try:
        size = len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise cd.ControlValueError("Invalid expected state.") from None
    if size > MAX_EXPECT_BYTES:
        raise cd.ControlValueError("The expected state is too large.")
    result = {}
    for target, entry in raw.items():
        parsed = cd.parse_target(target)
        if target not in changes or not parsed or not isinstance(entry, dict):
            raise cd.ControlValueError("Invalid expected state.")
        source, _, field = parsed
        if source == "binding" and field in ("value", "seed_mode"):
            if set(entry) - {"prior", "targets"} or "targets" not in entry:
                raise cd.ControlValueError("Invalid expected state.")
            result[target] = {"targets": _paths(entry["targets"], pairs=True),
                              **({"prior": entry["prior"]} if "prior" in entry else {})}
        elif source == "execution" and field == "mode":
            if set(entry) != {"prior", "members"} or entry["prior"] not in (*cm.EXECUTION_MODES, "mixed"):
                raise cd.ControlValueError("Invalid expected state.")
            result[target] = {"prior": entry["prior"], "members": _paths(entry["members"], pairs=False)}
        elif source == "preset" and field == "apply":
            token = entry.get("token")
            if set(entry) != {"token"} or not isinstance(token, str) or not 0 < len(token) <= 64:
                raise cd.ControlValueError("Invalid expected state.")
            result[target] = {"token": token}
        else:
            raise cd.ControlValueError("Invalid expected state.")
    return result


def _pairs(targets: list) -> list:
    return sorted([str(t.get("path")), str(t.get("input"))] for t in targets or [])


def _same(descriptor: dict, a: Any, b: Any) -> bool:
    if descriptor.get("kind") == control_rows.ROWS_KIND:
        return control_rows.canonical(a) == control_rows.canonical(b)
    try:
        return cd.coerce_binding_value(descriptor, a) == cd.coerce_binding_value(descriptor, b)
    except (cd.ControlValueError, TypeError, ValueError, OverflowError):
        return a == b or (a is not None and b is not None and str(a) == str(b))


def _shown(value: Any) -> str:
    text = "on" if value is True else "off" if value is False else "empty" if value in (None, "") else str(value)
    return text if len(text) <= 60 else text[:59] + "…"


def check(view: dict, changes: dict, expect: dict) -> list[dict]:
    """Refusals for targets whose state is no longer what the gesture saw.

    A target whose current state already equals the requested one passes (the
    same edit landed from elsewhere). Targets the batch validation refuses for
    their own reason (unavailable, linked) are left to it."""
    if not expect:
        return []
    bindings = {d["binding_id"]: d for d in view.get("bindings", [])}
    switches = {d["execution_id"]: d for d in view.get("execution", []) or []}
    presets = {row.get("id"): row for row in view.get("presets", []) or []}
    rejected = []
    for target, entry in expect.items():
        source, identity, field = cd.parse_target(target)
        if source == "binding":
            descriptor = bindings.get(identity)
            if not descriptor or descriptor.get("status") != "ok":
                continue
            label = descriptor.get("label") or identity
            if _pairs(descriptor.get("targets")) != sorted(entry["targets"]):
                rejected.append({"target": target, "reason": (
                    f"“{label}” now sets a different input. Check it and try again.")})
                continue
            if "prior" not in entry:
                continue
            if field == "seed_mode":
                current = descriptor.get("seed_mode")
                if current not in (entry["prior"], changes[target]):
                    rejected.append({"target": target, "reason": (
                        f"“{label}” was set to {_shown(current)} elsewhere.")})
                continue
            # A row list's own base check covers it; an advancing seed moves on every run.
            if descriptor.get("kind") == control_rows.ROWS_KIND or (
                    descriptor.get("kind") == "SEED" and descriptor.get("seed_mode") not in (None, "fixed")):
                continue
            current = descriptor.get("value")
            if not _same(descriptor, current, entry["prior"]) and not _same(descriptor, current, changes[target]):
                rejected.append({"target": target, "reason": (
                    f"“{label}” was changed to {_shown(current)} elsewhere.")})
        elif source == "execution":
            descriptor = switches.get(identity)
            if not descriptor or descriptor.get("status") != "ok":
                continue
            label = descriptor.get("label") or identity
            if sorted(str(m.get("path")) for m in descriptor.get("members") or []) != sorted(entry["members"]):
                rejected.append({"target": target, "reason": (
                    f"“{label}” now covers different nodes. Check it and try again.")})
                continue
            current = descriptor.get("mode")
            if current not in (entry["prior"], changes[target]):
                rejected.append({"target": target, "reason": (
                    f"“{label}” was switched to {str(current).capitalize()} elsewhere.")})
        elif source == "preset":
            row = presets.get(identity)
            if row is None or row.get("token") is None:
                continue
            if row["token"] != entry["token"]:
                rejected.append({"target": target, "reason": (
                    f"“{row.get('name') or identity}” changed after you clicked it, so nothing was applied. "
                    "Click it again to apply it as it is now.")})
    return rejected
