"""What the Launcher remembers for this ComfyUI install: hidden items and last workflows.

Install-level, never in a project and never in the browser, so every browser that
opens the Launcher agrees (``<comfy user dir>/sonder-editor/launcher.json``)::

    {"version": 1,
     "hidden": {"projects": [<folder>], "workflows": [<template_id>]},
     "last_workflow": {"by_project": {<folder>: {"template_id", "at"}},
                       "by_template": {<template_id>: <at>},
                       "any": {"template_id", "at"}}}

Projects are keyed by their **folder name**, the key every project route and SDK
session uses. ``project_id`` cannot be the key: a copied project folder keeps its
id, so several projects can share one. Renaming a folder therefore forgets that
project's entries. Times are Unix seconds. Each map keeps its newest
``MAX_ENTRIES`` entries. Unknown keys are kept, and an unreadable file is moved
aside by ``read_json_sidecar`` and the Launcher starts fresh.
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, Callable

from .path_security import PathSecurityError, safe_route_token
from .sdk_paths import json_sidecar_lock, read_json_sidecar, sonder_user_dir, write_json_atomic

STATE_NAME = "launcher.json"
STATE_VERSION = 1
MAX_ENTRIES = 500
MAX_FOLDER_LENGTH = 255
TEMPLATE_ID_RE = re.compile(r"^[0-9a-f]{16}$")
KINDS = ("projects", "workflows")


class LauncherStateError(ValueError):
    """A request naming something the Launcher cannot remember."""


class LauncherStateUnavailable(RuntimeError):
    """Nowhere to remember it: ComfyUI's user directory is unavailable."""


def state_path() -> str:
    base = sonder_user_dir()
    return os.path.join(base, STATE_NAME) if base else ""


def check_folder(folder: Any) -> str:
    try:
        value = safe_route_token(str(folder or ""), "project")
    except PathSecurityError as exc:
        raise LauncherStateError("Unknown project.") from exc
    if len(value) > MAX_FOLDER_LENGTH:
        raise LauncherStateError("Unknown project.")
    return value


def check_template_id(template_id: Any) -> str:
    value = str(template_id or "")
    if not TEMPLATE_ID_RE.match(value):
        raise LauncherStateError("Unknown workflow.")
    return value


def _clean_time(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def _valid_folder(value: Any) -> bool:
    try:
        check_folder(value)
        return True
    except LauncherStateError:
        return False


def normalize(data: Any) -> dict:
    """The stored state with every known field repaired; unknown keys are kept."""
    data = dict(data) if isinstance(data, dict) else {}
    raw_hidden = data.get("hidden") if isinstance(data.get("hidden"), dict) else {}
    hidden = dict(raw_hidden)
    for kind, valid in (("projects", _valid_folder), ("workflows", lambda v: bool(TEMPLATE_ID_RE.match(str(v))))):
        items = raw_hidden.get(kind) if isinstance(raw_hidden.get(kind), list) else []
        seen: list[str] = []
        for item in items:
            if isinstance(item, str) and valid(item) and item not in seen:
                seen.append(item)
        hidden[kind] = seen[-MAX_ENTRIES:]

    raw_last = data.get("last_workflow") if isinstance(data.get("last_workflow"), dict) else {}
    last = dict(raw_last)
    by_project = {}
    raw_by_project = raw_last.get("by_project") if isinstance(raw_last.get("by_project"), dict) else {}
    for folder, entry in raw_by_project.items():
        if not (_valid_folder(folder) and isinstance(entry, dict)):
            continue
        template_id = str(entry.get("template_id") or "")
        if TEMPLATE_ID_RE.match(template_id):
            by_project[folder] = {"template_id": template_id, "at": _clean_time(entry.get("at"))}
    last["by_project"] = dict(sorted(by_project.items(), key=lambda kv: kv[1]["at"])[-MAX_ENTRIES:])
    raw_by_template = raw_last.get("by_template") if isinstance(raw_last.get("by_template"), dict) else {}
    by_template = {key: _clean_time(at) for key, at in raw_by_template.items() if TEMPLATE_ID_RE.match(str(key))}
    last["by_template"] = dict(sorted(by_template.items(), key=lambda kv: kv[1])[-MAX_ENTRIES:])
    any_entry = raw_last.get("any") if isinstance(raw_last.get("any"), dict) else {}
    any_template = str(any_entry.get("template_id") or "")
    last["any"] = ({"template_id": any_template, "at": _clean_time(any_entry.get("at"))}
                   if TEMPLATE_ID_RE.match(any_template) else {})

    data.update(version=STATE_VERSION, hidden=hidden, last_workflow=last)
    return data


def read_state() -> dict:
    path = state_path()
    if not path:
        return normalize({})
    with json_sidecar_lock(path):
        return normalize(read_json_sidecar(path))


def _update(mutate: Callable[[dict], None]) -> dict:
    path = state_path()
    if not path:
        raise LauncherStateUnavailable("ComfyUI's user directory is unavailable.")
    with json_sidecar_lock(path):
        data = normalize(read_json_sidecar(path))
        mutate(data)
        data = normalize(data)
        write_json_atomic(path, data)
        return data


def set_hidden(kind: str, item_id: Any, hidden: bool) -> dict:
    if kind == "project":
        key, value = "projects", check_folder(item_id)
    elif kind == "workflow":
        key, value = "workflows", check_template_id(item_id)
    else:
        raise LauncherStateError("Unknown kind.")

    def mutate(data: dict) -> None:
        items = [item for item in data["hidden"][key] if item != value]
        if hidden:
            items.append(value)
        data["hidden"][key] = items

    return _update(mutate)


def record_open(folder: Any, template_id: Any, *, clock: Callable[[], float] = time.time) -> dict:
    """Remember that ``template_id`` was opened on project ``folder``."""
    folder, template_id = check_folder(folder), check_template_id(template_id)
    now = float(clock())

    def mutate(data: dict) -> None:
        last = data["last_workflow"]
        last["by_project"].pop(folder, None)  # re-insert so it counts as newest
        last["by_project"][folder] = {"template_id": template_id, "at": now}
        last["by_template"][template_id] = now
        last["any"] = {"template_id": template_id, "at": now}

    return _update(mutate)


def preferred_workflow(state: dict, folder: str, available: list[str]) -> str:
    """The workflow to preselect for ``folder``: its last one, else the last used anywhere,
    else the first available. Hidden or deleted workflows are skipped; "" when none fits."""
    hidden = set(state["hidden"]["workflows"])
    usable = [template_id for template_id in available if template_id not in hidden]
    last = state["last_workflow"]
    for candidate in (last["by_project"].get(folder, {}).get("template_id", ""),
                      last["any"].get("template_id", "")):
        if candidate and candidate in usable:
            return candidate
    return usable[0] if usable else ""


def public_state(state: dict) -> dict:
    """What the Launcher page reads: hidden ids and last-workflow times."""
    last = state["last_workflow"]
    return {
        "hidden": {kind: list(state["hidden"][kind]) for kind in KINDS},
        "last_workflow": {
            "by_project": {folder: dict(entry) for folder, entry in last["by_project"].items()},
            "by_template": dict(last["by_template"]),
            "any": dict(last["any"]),
        },
    }
