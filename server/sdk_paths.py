"""The ``<comfy user dir>/sonder-editor`` folder and its small JSON side files.

Install-level state lives here, never in a project: the server settings, SDK
templates and their Control values, the SDK editor position per project, and the
SDK install record. Writes are atomic (``atomic_io.atomic_replace``) under a
per-file lock. An unreadable file is moved aside, never silently overwritten,
because some of these files hold the user's choices.

Import discipline: stdlib + ``atomic_io`` at module level; ``folder_paths`` lazily,
so ``external_links`` can delegate here without an import cycle.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from typing import Any

from .atomic_io import atomic_replace

logger = logging.getLogger("sonder_editor")

_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}


def sonder_user_dir() -> str:
    """``<comfy user dir>/sonder-editor``, or "" when ComfyUI's user dir is unavailable."""
    try:
        import folder_paths

        user_dir = folder_paths.get_user_directory()
    except Exception:
        return ""
    if not user_dir:
        return ""
    return os.path.join(user_dir, "sonder-editor")


def json_sidecar_lock(path: str) -> threading.RLock:
    """The process-wide lock for one side file (keyed by its normalized real path)."""
    key = os.path.normcase(os.path.realpath(path))
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.RLock()
        return lock


def write_json_atomic(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        atomic_replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_json_sidecar(path: str) -> dict[str, Any]:
    """The file's object; ``{}`` when missing. A file that does not parse is quarantined.

    Quarantining (renaming to ``*.corrupt-<time>``) keeps the bytes for a person to
    recover while letting the caller start fresh, instead of a later write
    replacing them with nothing. Only bad *content* is quarantined: an ``OSError``
    (a sharing violation, an antivirus or indexer handle) is raised as is, since
    the file itself may be fine.
    """
    if not os.path.isfile(path):
        return {}
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        if isinstance(data, dict):
            return data
        problem = "not a JSON object"
    except ValueError as exc:  # includes UnicodeDecodeError and JSONDecodeError
        problem = str(exc)
    aside = f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    try:
        os.replace(path, aside)
        logger.warning("Moved unreadable Sonder side file %s aside to %s: %s", path, aside, problem)
    except OSError as exc:
        logger.warning("Unreadable Sonder side file %s (%s) could not be moved aside: %s", path, problem, exc)
        raise
    return {}
