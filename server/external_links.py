"""Opt-in trust mode for external project links (symlinks/junctions) + link mechanics.

The editor's containment checks are realpath-based ("hardened"): they refuse to follow any
symlink or junction, which also blocks the legitimate workflow of keeping a project — or
individual media assets — on another drive. This module owns the consent switch and the link
mechanics for that workflow:

- ``is_enabled()`` — the install-level "Allow external project links" flag. Server-side by
  design (it gates server trust; a browser-local setting could not be enforced). Stored at
  ``<comfy user dir>/sonder-editor/settings.json``; default OFF. When ON, ``path_security``
  switches its containment checks to lexical comparison so links the user planted are
  followed *everywhere the editor resolves files*.
- ``create_project_link`` / ``remove_project_link`` — "the symlink IS the registry": linking a
  project means creating a directory junction (or symlink) under ``<output>/sonder-projects``;
  unlinking removes only the reparse point, never the target's files.
- ``is_reparse_child`` — child-level reparse detection that stays correct even when the scan
  root itself sits behind a symlink (as it does under Stability Matrix's output→Images link).

Import discipline: stdlib + ``atomic_io`` at module level; ``folder_paths`` and
``path_security`` are imported lazily inside functions (``path_security`` lazily imports
``is_enabled`` from here, so a module-level import back would be a cycle).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid

from .atomic_io import atomic_replace

logger = logging.getLogger("sonder_editor")

SETTINGS_VERSION = 1
_FLAG_KEY = "allow_external_project_links"

# The flag is consulted from path_within(), which runs per-entry inside the /assets media
# snapshot loop (thousands of calls per request) — a per-call os.stat of settings.json is
# too hot, so reads are TTL-cached. set_enabled() invalidates immediately (live toggle).
_FLAG_CACHE_TTL_SEC = 1.0
_flag_cache = {"value": False, "expires": 0.0}

# Windows device names are rejected explicitly in the lexical trust path (realpath-based
# containment used to catch them incidentally).
WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class LinkError(ValueError):
    """A link/unlink request that must be refused with a specific HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ── Flag store ────────────────────────────────────────────────────────────────


def _settings_path() -> str:
    try:
        import folder_paths

        user_dir = folder_paths.get_user_directory()
    except Exception:
        return ""
    if not user_dir:
        return ""
    return os.path.join(user_dir, "sonder-editor", "settings.json")


def _read_settings() -> dict:
    path = _settings_path()
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("external_links: failed to read settings %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def is_enabled() -> bool:
    """Whether external links are trusted. Fail-safe OFF on any error (incl. no ComfyUI)."""
    now = time.monotonic()
    if now < _flag_cache["expires"]:
        return _flag_cache["value"]
    try:
        value = bool(_read_settings().get(_FLAG_KEY, False))
    except Exception:
        value = False
    _flag_cache["value"] = value
    _flag_cache["expires"] = now + _FLAG_CACHE_TTL_SEC
    return value


def set_enabled(value: bool) -> None:
    path = _settings_path()
    if not path:
        raise LinkError("Cannot save server settings: ComfyUI user directory unavailable", 500)
    data = _read_settings()
    data["version"] = SETTINGS_VERSION
    data[_FLAG_KEY] = bool(value)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    atomic_replace(tmp, path)
    _flag_cache["value"] = bool(value)
    _flag_cache["expires"] = time.monotonic() + _FLAG_CACHE_TTL_SEC
    logger.info("external_links: allow_external_project_links set to %s", bool(value))


def invalidate_flag_cache() -> None:
    """Force the next is_enabled() to re-read the settings file (tests / external edits)."""
    _flag_cache["expires"] = 0.0


# ── Reparse detection ─────────────────────────────────────────────────────────


def is_reparse_child(parent: str, name: str) -> bool:
    """True when ``<parent>/<name>`` is itself a symlink/junction (child-level only).

    Comparing ``realpath(child)`` against ``realpath(parent)/name`` (rather than against
    ``abspath(child)``) keeps this correct when the *parent chain* is behind a link — e.g.
    the Stability Matrix output→Images symlink above the scan root — which must NOT make
    every real project look linked.
    """
    child = os.path.join(str(parent or ""), str(name or ""))
    if not name or not os.path.lexists(child):
        return False
    # Primary detector: readlink succeeds for symlinks AND junctions (Windows, Py>=3.8)
    # and raises OSError for real files/dirs.
    try:
        os.readlink(child)
        return True
    except OSError:
        pass
    except ValueError:
        return False
    try:
        child_real = os.path.normcase(os.path.realpath(child))
        expected = os.path.normcase(os.path.join(os.path.realpath(parent), name))
        return child_real != expected
    except (OSError, ValueError):
        return False


# ── Link mechanics ────────────────────────────────────────────────────────────


def scan_root() -> str:
    """``<comfy_output>/sonder-projects`` — realpath'd once (the anchor root)."""
    try:
        import folder_paths

        base = os.path.join(folder_paths.get_output_directory(), "sonder-projects")
    except Exception:
        return ""
    return os.path.realpath(base) if base else ""


def _validate_link_name(name: str) -> str:
    from .path_security import PathSecurityError, safe_route_token

    # Check these before safe_route_token: trust-mode path validation also rejects
    # device names, but this link-specific route should retain a clear repair hint.
    if name != name.rstrip(". "):
        raise LinkError("Folder names ending in a dot or space are not usable on Windows")
    stem = name.split(".", 1)[0].strip().upper()
    if stem in WINDOWS_RESERVED_BASENAMES:
        raise LinkError(f"'{name}' is a reserved Windows device name")
    try:
        safe_route_token(name, "link name")
    except PathSecurityError as exc:
        raise LinkError(f"Folder name '{name}' cannot be used as a project id ({exc}) — rename the folder first") from exc
    if name != name.rstrip(". "):
        raise LinkError("Folder names ending in a dot or space are not usable on Windows — rename the folder first")
    stem = name.split(".", 1)[0].strip().upper()
    if stem in WINDOWS_RESERVED_BASENAMES:
        raise LinkError(f"'{name}' is a reserved Windows device name — rename the folder first")
    return name


def _create_dir_link(link_path: str, target: str) -> None:
    if os.name == "nt":
        # Junctions need no privilege (local volumes). Fall back to a real directory
        # symlink for UNC/network targets, which needs Developer Mode or admin rights.
        try:
            import _winapi

            _winapi.CreateJunction(target, link_path)
            return
        except OSError as junction_exc:
            try:
                os.symlink(target, link_path, target_is_directory=True)
                return
            except OSError as symlink_exc:
                raise LinkError(
                    "Could not create the link. Junctions only work for local drives "
                    f"(error: {junction_exc}); a symlink needs Windows Developer Mode or an "
                    f"elevated ComfyUI (error: {symlink_exc}).",
                    500,
                ) from symlink_exc
    try:
        os.symlink(target, link_path, target_is_directory=True)
    except OSError as exc:
        raise LinkError(f"Could not create the link: {exc}", 500) from exc


def create_project_link(raw_path: str) -> dict:
    """Create ``<sonder-projects>/<basename(target)>`` as a junction/symlink to ``target``.

    Refusals carry an HTTP status via ``LinkError``: flag OFF (403), target missing (404),
    not a project / bad name (400), name collision (409).
    """
    if not is_enabled():
        raise LinkError(
            "External project links are disabled. Enable 'Allow external project links' in Settings first.",
            403,
        )
    raw = str(raw_path or "").strip().strip('"')
    if not raw or "\x00" in raw:
        raise LinkError("A folder path is required")

    target = os.path.realpath(raw)
    if not os.path.isdir(target):
        raise LinkError("That path is not a folder on the server", 404)
    if not os.path.isfile(os.path.join(target, "project.json")):
        raise LinkError("That folder is not a Sonder project (no project.json inside)")

    root = scan_root()
    if not root:
        raise LinkError("Projects folder unavailable", 500)
    try:
        if os.path.commonpath([os.path.normcase(root), os.path.normcase(target)]) == os.path.normcase(root):
            raise LinkError("That project is already in the projects folder")
    except ValueError:
        pass  # different drives — definitely not inside the scan root

    name = os.path.basename(os.path.normpath(target))
    _validate_link_name(name)
    link_path = os.path.join(root, name)
    if os.path.lexists(link_path):
        raise LinkError(f"'{name}' already exists in the projects folder — rename one of them first", 409)

    os.makedirs(root, exist_ok=True)
    _create_dir_link(link_path, target)
    logger.info("external_links: linked %s -> %s", link_path, target)
    return {"name": name, "path": link_path, "target": target}


def remove_project_link(raw_name: str) -> bool:
    """Remove the reparse point ``<sonder-projects>/<name>``. Never touches target files.

    Deliberately allowed even when the trust flag is OFF (removing trust surface is always
    fine) and works for dangling links (whose projects no longer list anywhere).
    """
    from .path_security import PathSecurityError, safe_route_token

    try:
        name = safe_route_token(str(raw_name or ""), "project link name")
    except PathSecurityError as exc:
        raise LinkError(str(exc)) from exc
    root = scan_root()
    if not root:
        raise LinkError("Projects folder unavailable", 500)
    link_path = os.path.join(root, name)
    if not os.path.lexists(link_path):
        return False
    if not is_reparse_child(root, name):
        raise LinkError(f"'{name}' is a real project folder, not a link — refusing to remove it", 409)
    # os.rmdir removes only the reparse point for junctions/Windows dir-symlinks and ERRORS
    # on a non-empty real directory (second safety net); POSIX dir symlinks need unlink.
    try:
        os.rmdir(link_path)
    except OSError:
        try:
            os.unlink(link_path)
        except OSError as exc:
            raise LinkError(f"Could not remove the link: {exc}", 500) from exc
    logger.info("external_links: unlinked %s", link_path)
    return True
