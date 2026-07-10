"""Shared filesystem containment helpers for Sonder Editor."""

from __future__ import annotations

import logging
import ntpath
import os
import posixpath
import re

logger = logging.getLogger("sonder_editor")

_ENCODED_SEPARATOR_RE = re.compile(r"%(?:2f|5c)", re.IGNORECASE)
_LOGGED_QUARANTINES: set[tuple[str, str, str, str]] = set()
_WINDOWS_RESERVED_BASENAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class PathSecurityError(ValueError):
    """Raised when a path would escape its intended containment root."""


def _external_links_enabled() -> bool:
    """Read the install-level opt-in without making this module import-cycle prone."""
    try:
        from .external_links import is_enabled

        return bool(is_enabled())
    except Exception:
        return False


def _is_windows_reserved_basename(value: str) -> bool:
    """Whether a path component spells a Windows device file (including ``CON.txt``)."""
    cleaned = str(value or "").rstrip(". ")
    stem = cleaned.split(".", 1)[0].upper()
    return stem in _WINDOWS_RESERVED_BASENAMES


def path_within(parent: str, child: str) -> bool:
    try:
        if _external_links_enabled():
            # Trust-on spelling family: the caller supplies an anchor that has already
            # been realpath'd once, then every descendant remains lexical. Re-realpathing
            # a child here would erase a project junction and mix path families.
            parent_path = os.path.normcase(os.path.abspath(str(parent or "")))
            child_path = os.path.normcase(os.path.abspath(str(child or "")))
        else:
            parent_path = os.path.normcase(os.path.realpath(str(parent or "")))
            child_path = os.path.normcase(os.path.realpath(str(child or "")))
        return os.path.commonpath([parent_path, child_path]) == parent_path
    except (OSError, ValueError):
        return False


def safe_route_token(value: str, label: str) -> str:
    value = str(value or "").strip()
    if (
        not value
        or "/" in value
        or "\\" in value
        or "\x00" in value
        or ".." in value
        or ":" in value
        or _ENCODED_SEPARATOR_RE.search(value)
    ):
        raise PathSecurityError(f"Invalid {label}")
    if _external_links_enabled() and _is_windows_reserved_basename(value):
        raise PathSecurityError(f"Invalid {label}")
    return value


def sanitize_filename_component(value: str, fallback: str = "file") -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    component = re.sub(r"_+", "_", component).strip("._-")
    return component[:120] or fallback


def normalize_project_relative_path(path: str, *, allow_empty: bool = False) -> str:
    raw = str(path or "")
    if raw == "":
        if allow_empty:
            return ""
        raise PathSecurityError("Path is required")
    if "\x00" in raw:
        raise PathSecurityError("Path contains NUL")
    if _ENCODED_SEPARATOR_RE.search(raw):
        raise PathSecurityError("Path contains encoded separator")
    if raw != raw.strip():
        raise PathSecurityError("Path has leading or trailing whitespace")
    if posixpath.isabs(raw) or ntpath.isabs(raw):
        raise PathSecurityError("Absolute paths are not allowed")
    drive, _tail = ntpath.splitdrive(raw)
    if drive:
        raise PathSecurityError("Drive-qualified paths are not allowed")

    normalized = raw.replace("\\", "/")
    if ":" in normalized:
        raise PathSecurityError("Colon path components are not allowed")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise PathSecurityError("Traversal path components are not allowed")
    if _external_links_enabled() and any(_is_windows_reserved_basename(part) for part in parts):
        raise PathSecurityError("Windows reserved path components are not allowed")
    return "/".join(parts)


def log_path_quarantine(
    *,
    purpose: str,
    path: str,
    root: str = "",
    reason: str = "",
) -> None:
    root_key = os.path.normcase(os.path.realpath(str(root or "")))
    path_key = str(path or "")[:500]
    reason_key = str(reason or "uncontained")
    purpose_key = str(purpose or "path")
    key = (purpose_key, path_key, root_key, reason_key)
    if key in _LOGGED_QUARANTINES:
        logger.debug(
            "Security quarantine repeated: purpose=%s reason=%s path=%r root=%s",
            purpose_key,
            reason_key,
            path_key,
            root_key,
        )
        return
    _LOGGED_QUARANTINES.add(key)
    logger.warning(
        "Security quarantine: purpose=%s reason=%s path=%r root=%s",
        purpose_key,
        reason_key,
        path_key,
        root_key,
    )


def resolve_under_root(
    root: str,
    relative_path: str,
    *,
    purpose: str = "project path",
    must_exist: bool = False,
    log: bool = True,
) -> str:
    try:
        rel = normalize_project_relative_path(relative_path)
        if _external_links_enabled():
            # ``root`` is a spelling-family anchor (e.g. real scan root + junction
            # basename). Keep the lexical descendant so project links remain usable.
            root_path = os.path.abspath(str(root or ""))
            target = os.path.abspath(os.path.join(root_path, *rel.split("/")))
            if not path_within(root_path, target):
                raise PathSecurityError("Resolved path escapes root")
            if must_exist and not os.path.exists(target):
                return ""
            return target

        root_real = os.path.realpath(str(root or ""))
        target = os.path.abspath(os.path.join(root_real, *rel.split("/")))
        target_real = os.path.realpath(target)
        if not path_within(root_real, target_real):
            raise PathSecurityError("Resolved path escapes root")
        if must_exist and not os.path.exists(target_real):
            return ""
        return target_real
    except PathSecurityError as exc:
        if log:
            log_path_quarantine(
                purpose=purpose,
                path=str(relative_path or ""),
                root=str(root or ""),
                reason=str(exc),
            )
        return ""


def resolve_project_path(
    project_or_dir,
    relative_path: str,
    *,
    purpose: str = "project path",
    must_exist: bool = False,
    log: bool = True,
) -> str:
    project_dir = project_or_dir if isinstance(project_or_dir, str) else getattr(project_or_dir, "project_dir", "")
    return resolve_under_root(
        project_dir,
        relative_path,
        purpose=purpose,
        must_exist=must_exist,
        log=log,
    )


def project_root(project_or_dir, *, must_exist: bool = False) -> str:
    project_dir = project_or_dir if isinstance(project_or_dir, str) else getattr(project_or_dir, "project_dir", "")
    root = str(project_dir or "")
    if not root or "\x00" in root:
        log_path_quarantine(
            purpose="project root",
            path=root,
            reason="invalid project root",
        )
        return ""
    root_path = os.path.abspath(root) if _external_links_enabled() else os.path.realpath(root)
    if must_exist and not os.path.isdir(root_path):
        return ""
    return root_path


def resolve_existing_project_path(
    project_or_dir,
    relative_path: str,
    *,
    purpose: str = "project path",
    log: bool = True,
) -> str:
    return resolve_project_path(
        project_or_dir,
        relative_path,
        purpose=purpose,
        must_exist=True,
        log=log,
    )


def _project_dir_value(project_or_dir) -> str:
    return project_or_dir if isinstance(project_or_dir, str) else getattr(project_or_dir, "project_dir", "")


def _project_subroot(
    project_or_dir,
    subroot_relative_path: str,
    *,
    purpose: str,
    must_exist: bool = False,
    log: bool = True,
) -> str:
    try:
        rel = normalize_project_relative_path(subroot_relative_path)
        root = project_root(project_or_dir)
        if not root:
            return ""
        target = os.path.abspath(os.path.join(root, *rel.split("/")))
        if not path_within(root, target):
            raise PathSecurityError("Subroot escapes project")
        if not _external_links_enabled():
            target_real = os.path.realpath(target)
            if os.path.normcase(target_real) != os.path.normcase(target):
                raise PathSecurityError("Subroot resolves outside its canonical project location")
        else:
            target_real = target
        if os.path.lexists(target) and not os.path.isdir(target):
            raise PathSecurityError("Subroot is not a directory")
        if must_exist and not os.path.isdir(target):
            return ""
        return target_real
    except PathSecurityError as exc:
        if log:
            root = _project_dir_value(project_or_dir)
            log_path_quarantine(
                purpose=purpose,
                path=str(subroot_relative_path or ""),
                root=str(root or ""),
                reason=str(exc),
            )
        return ""


def project_media_root(project_or_dir, *, must_exist: bool = False) -> str:
    return _project_subroot(
        project_or_dir,
        "media",
        purpose="project media root",
        must_exist=must_exist,
    )


def project_cache_root(project_or_dir, *, must_exist: bool = False) -> str:
    return _project_subroot(
        project_or_dir,
        "cache",
        purpose="project cache root",
        must_exist=must_exist,
    )


def project_cache_path(
    project_or_dir,
    cache_relative_path: str,
    *,
    purpose: str = "project cache path",
    must_exist: bool = False,
    log: bool = True,
) -> str:
    try:
        normalized = normalize_project_relative_path(cache_relative_path)
        if normalized == "cache" or normalized.startswith("cache/"):
            rel = normalized[6:] if normalized.startswith("cache/") else ""
        else:
            rel = normalized
    except PathSecurityError as exc:
        if log:
            root = _project_dir_value(project_or_dir)
            log_path_quarantine(
                purpose=purpose,
                path=str(cache_relative_path or ""),
                root=str(root or ""),
                reason=str(exc),
            )
        return ""
    root = project_cache_root(project_or_dir, must_exist=must_exist and not rel)
    if not root:
        return ""
    if not rel:
        return root
    return resolve_under_root(
        root,
        rel,
        purpose=purpose,
        must_exist=must_exist,
        log=log,
    )


def project_bridge_root(project_or_dir, *, must_exist: bool = False) -> str:
    return _project_subroot(
        project_or_dir,
        "cache/bridge_out",
        purpose="project bridge output root",
        must_exist=must_exist,
    )


def project_bridge_path(
    project_or_dir,
    bridge_relative_path: str,
    *,
    purpose: str = "project bridge output path",
    must_exist: bool = False,
    log: bool = True,
) -> str:
    try:
        normalized = normalize_project_relative_path(bridge_relative_path)
        if normalized == "cache/bridge_out" or normalized.startswith("cache/bridge_out/"):
            rel = normalized[len("cache/bridge_out/"):] if normalized.startswith("cache/bridge_out/") else ""
        elif normalized == "bridge_out" or normalized.startswith("bridge_out/"):
            rel = normalized[len("bridge_out/"):] if normalized.startswith("bridge_out/") else ""
        else:
            rel = normalized
    except PathSecurityError as exc:
        if log:
            root = _project_dir_value(project_or_dir)
            log_path_quarantine(
                purpose=purpose,
                path=str(bridge_relative_path or ""),
                root=str(root or ""),
                reason=str(exc),
            )
        return ""
    root = project_bridge_root(project_or_dir, must_exist=must_exist and not rel)
    if not root:
        return ""
    if not rel:
        return root
    return resolve_under_root(
        root,
        rel,
        purpose=purpose,
        must_exist=must_exist,
        log=log,
    )


def resolve_static_path(
    static_root: str,
    static_relative_path: str,
    *,
    purpose: str = "static file",
    must_exist: bool = True,
    log: bool = True,
) -> str:
    return resolve_under_root(
        static_root,
        static_relative_path,
        purpose=purpose,
        must_exist=must_exist,
        log=log,
    )


def comfy_input_root(*, must_exist: bool = False) -> str:
    try:
        import folder_paths

        root = folder_paths.get_input_directory()
    except Exception as exc:
        log_path_quarantine(
            purpose="comfy input root",
            path="",
            reason=f"unavailable: {exc}",
        )
        return ""
    root_real = os.path.realpath(str(root or ""))
    if must_exist and not os.path.isdir(root_real):
        return ""
    return root_real


def resolve_comfy_input_path(
    input_relative_path: str,
    *,
    input_root: str = "",
    purpose: str = "comfy input file",
    must_exist: bool = True,
    log: bool = True,
) -> str:
    root = input_root or comfy_input_root(must_exist=must_exist)
    if not root:
        return ""
    return resolve_under_root(
        root,
        input_relative_path,
        purpose=purpose,
        must_exist=must_exist,
        log=log,
    )


def project_media_path(
    project_or_dir,
    media_relative_path: str,
    *,
    purpose: str = "project media path",
    must_exist: bool = False,
    log: bool = True,
) -> str:
    try:
        normalized = normalize_project_relative_path(media_relative_path)
        if normalized == "media" or normalized.startswith("media/"):
            rel = normalized[6:] if normalized.startswith("media/") else ""
        else:
            rel = normalized
    except PathSecurityError as exc:
        if log:
            root = _project_dir_value(project_or_dir)
            log_path_quarantine(
                purpose=purpose,
                path=str(media_relative_path or ""),
                root=str(root or ""),
                reason=str(exc),
            )
        return ""
    root = project_media_root(project_or_dir, must_exist=must_exist and not rel)
    if not root:
        return ""
    if not rel:
        return root
    return resolve_under_root(
        root,
        rel,
        purpose=purpose,
        must_exist=must_exist,
        log=log,
    )
