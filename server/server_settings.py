"""Server-wide Sonder settings: the only reader and writer of
``<comfy user dir>/sonder-editor/settings.json``.

Two settings live here, both for this ComfyUI installation rather than a browser or a project:

- ``allow_external_project_links`` — consumed by ``external_links.is_enabled`` (trust).
- ``share_projects_between_servers`` — consumed by ``project_manager._project_commit_lock``:
  whether saves also take the cross-process OS lock (``project_file_lock``).

A read has three outcomes. *Absent* (no file, or no ComfyUI user directory) gives the
defaults. *Valid* (a JSON object) gives its values. *Indeterminate* (any other failure)
fails safe per key: links read as off, so trust stays closed, and sharing reads as on, so
protection stays on, which costs nothing on a drive that supports file locks. There is no
"last good value": a restart has none, and a value another writer changed would be missed.

A write never replaces a file that cannot be read (`SettingsUnreadable`): a links change
would otherwise erase the sharing setting and any key a newer version added.

Reads are cached for ``CACHE_TTL_SEC``. A cache hit takes no lock, because
``path_security.path_within`` consults the links flag thousands of times per request. A miss
reads under the same lock as writes, and both publish one complete snapshot, so a read that
started before a write cannot publish its older values after it. A miss that finds the lock
taken answers with the expired values instead of waiting (it can be on the event loop),
unless there are none yet.

Two ComfyUI processes that share one user directory can lose one key when both write at
the same moment: the lock here is in-process only. Accepted; the settings change rarely.

Import discipline: stdlib + ``atomic_io`` + ``sdk_paths`` only (``external_links`` and
``project_manager`` import this).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid

from .atomic_io import atomic_replace
from .sdk_paths import sonder_user_dir

logger = logging.getLogger("sonder_editor")

SETTINGS_VERSION = 1
LINKS_KEY = "allow_external_project_links"
SHARING_KEY = "share_projects_between_servers"
KEYS = (LINKS_KEY, SHARING_KEY)

DEFAULTS = {LINKS_KEY: False, SHARING_KEY: False}
# What an unreadable file means for each key: trust stays closed, protection stays on.
INDETERMINATE = {LINKS_KEY: False, SHARING_KEY: True}

CACHE_TTL_SEC = 1.0

ABSENT, VALID, UNREADABLE = "absent", "valid", "indeterminate"

_lock = threading.Lock()
# Replaced whole, never mutated: a lock-free hit reads one consistent snapshot.
_snapshot = {"values": None, "expires": 0.0}
_warned_unreadable = False


class SettingsUnreadable(OSError):
    """The settings file exists but cannot be read, so nothing was written."""


class SettingsUnavailable(OSError):
    """There is no ComfyUI user directory to keep the settings in."""


def settings_path() -> str:
    root = sonder_user_dir()
    return os.path.join(root, "settings.json") if root else ""


def _read_document():
    """``(ABSENT, None)``, ``(VALID, dict)`` or ``(UNREADABLE, None)``."""
    path = settings_path()
    if not path:
        return ABSENT, None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return ABSENT, None
    except (OSError, ValueError) as exc:
        _warn_unreadable(path, exc)
        return UNREADABLE, None
    if not isinstance(data, dict):
        _warn_unreadable(path, "not a JSON object")
        return UNREADABLE, None
    return VALID, data


def _warn_unreadable(path, reason) -> None:
    global _warned_unreadable
    if not _warned_unreadable:
        _warned_unreadable = True
        logger.warning(
            "Sonder server settings %s cannot be read (%s): external project links are off "
            "and projects are protected as if shared between servers until it can be read.",
            path, reason)


def _values_from(state, data) -> dict:
    global _warned_unreadable
    if state == UNREADABLE:
        return dict(INDETERMINATE)
    _warned_unreadable = False
    if state == ABSENT:
        return dict(DEFAULTS)
    sharing = data.get(SHARING_KEY, False)
    # Only a literal true grants trust; a value that is not a boolean (a hand edit) leaves
    # protection on, as an unreadable file does.
    return {LINKS_KEY: data.get(LINKS_KEY) is True,
            SHARING_KEY: sharing if isinstance(sharing, bool) else True}


def _publish(values) -> None:
    global _snapshot
    _snapshot = {"values": values, "expires": time.monotonic() + CACHE_TTL_SEC}


def current() -> dict:
    """Every setting's effective value."""
    snapshot = _snapshot
    if snapshot["values"] is not None and time.monotonic() < snapshot["expires"]:
        return snapshot["values"]
    if not _lock.acquire(blocking=False):
        # A write is replacing the file (its retries can take ~0.5 s) or another reader is
        # reading it. Callers on the event loop must not wait: answer with the values held
        # until that write lands, without publishing them.
        if snapshot["values"] is not None:
            return snapshot["values"]
        _lock.acquire()
    try:
        snapshot = _snapshot  # a write may have published while this waited
        if snapshot["values"] is not None and time.monotonic() < snapshot["expires"]:
            return snapshot["values"]
        try:
            values = _values_from(*_read_document())
        except Exception:
            logger.exception("Reading Sonder server settings failed")
            values = dict(INDETERMINATE)
        _publish(values)
        return values
    finally:
        _lock.release()


def links_enabled() -> bool:
    return current()[LINKS_KEY]


def sharing_enabled() -> bool:
    return current()[SHARING_KEY]


def update(changes: dict) -> dict:
    """Write every key in ``changes`` in one replace, keeping all other keys; returns the
    new values. Raises `SettingsUnreadable` or `SettingsUnavailable` before writing."""
    unknown = set(changes) - set(KEYS)
    if unknown:
        raise ValueError(f"Unknown server settings: {sorted(unknown)}")
    with _lock:
        path = settings_path()
        if not path:
            raise SettingsUnavailable("ComfyUI user directory unavailable")
        state, data = _read_document()
        if state == UNREADABLE:
            raise SettingsUnreadable("The server settings file can't be read")
        document = dict(data) if state == VALID else {}
        document["version"] = SETTINGS_VERSION
        document.update({key: bool(value) for key, value in changes.items()})
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(document, handle, indent=2, ensure_ascii=False)
            atomic_replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        values = _values_from(VALID, document)
        _publish(values)
    for key, value in changes.items():
        logger.info("Sonder server setting %s set to %s", key, bool(value))
    return values


def invalidate_cache() -> None:
    """Make the next read go to the file (tests, or an edit made outside the server)."""
    global _snapshot
    _snapshot = {"values": None, "expires": 0.0}
