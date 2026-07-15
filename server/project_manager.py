import json
import os
import logging
import threading
import uuid
from datetime import datetime
from typing import Callable

from .atomic_io import atomic_replace
from . import external_links
from .path_security import path_within
from .timeline_state import TimelineProject

logger = logging.getLogger("sonder_editor")

PROJECT_SUBDIRS = [
    "media",
    os.path.join("media", "Exports"),
    "renders",
    os.path.join("cache", "thumbnails"),
    os.path.join("cache", "waveforms"),
    os.path.join("cache", "bridge_out"),
]

_PROJECT_SAVED_HOOKS: list[Callable[[TimelineProject], None]] = []

# Per-project write serialization. `save_project` runs from genuinely different OS
# threads — the ComfyUI prompt worker, the `sonder-bridge-*` daemon, `asyncio.to_thread`
# workers, and ~45 routes directly on the aiohttp event loop. File syscalls release the
# GIL, so concurrent read/replace on the single `project.json` collide on Windows
# (WinError 5 on os.replace, Errno 13 on the version-check open). This lock makes the
# read-version-check → tmp-write → atomic_replace a true compare-and-swap. It is a
# deliberate, scoped override of the former "no inter-thread lock" stance (see
# `atomic_io.py` and durable_rules): the bounded retry alone failed under
# editor-concurrent-with-render load. Held only around one uncontended save (low-ms,
# since serializing our own writers means os.replace succeeds first try) — never across
# the notify hooks — so the event loop is not stalled (route-blocking threshold is 0.5s).
# Keyed by the canonicalized path so the output→Images symlink's two spellings (and any
# `.`/case variance) map to ONE lock; otherwise the gate would not actually serialize.
_PROJECT_WRITE_LOCKS: dict[str, threading.Lock] = {}
_PROJECT_WRITE_LOCKS_GUARD = threading.Lock()


def _project_write_lock(project_dir: str) -> threading.Lock:
    try:
        key = os.path.normcase(os.path.realpath(project_dir))
    except OSError:
        key = os.path.normcase(os.path.abspath(project_dir))
        logger.warning(
            "save_project: realpath failed for %s; using abspath key — the write lock "
            "may not serialize across path spellings for this project.", project_dir,
        )
    with _PROJECT_WRITE_LOCKS_GUARD:
        lock = _PROJECT_WRITE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PROJECT_WRITE_LOCKS[key] = lock
        return lock


class ProjectVersionConflict(RuntimeError):
    """Raised when a caller tries to save over a newer project version."""

    def __init__(
        self,
        *,
        project_dir: str,
        expected_modified_at: str,
        actual_modified_at: str,
        current_data: dict | None = None,
    ):
        super().__init__("project_version_conflict")
        self.project_dir = project_dir
        self.expected_modified_at = expected_modified_at
        self.actual_modified_at = actual_modified_at
        self.current_data = current_data or {}


def register_project_saved_hook(hook: Callable[[TimelineProject], None]) -> None:
    if hook not in _PROJECT_SAVED_HOOKS:
        _PROJECT_SAVED_HOOKS.append(hook)


def create_project(
    name: str,
    fps: float = 24.0,
    width: int = 1280,
    height: int = 720,
    template_id: str = "free",
    base_dir: str = "",
) -> TimelineProject:
    if not base_dir:
        raise ValueError("base_dir must be specified")

    project_dir = os.path.join(base_dir, _safe_dirname(name))

    # BUG-1 fix: check if project already exists — load instead of overwriting
    project_file = os.path.join(project_dir, "project.json")
    if os.path.isfile(project_file):
        logger.info("Project '%s' already exists at %s — loading existing", name, project_dir)
        return load_project(project_dir)

    os.makedirs(project_dir, exist_ok=True)

    for subdir in PROJECT_SUBDIRS:
        os.makedirs(os.path.join(project_dir, subdir), exist_ok=True)

    project = TimelineProject(
        project_dir=project_dir,
        name=name,
        fps=fps,
        resolution=(width, height),
        template_id=template_id or "free",
    )

    save_project(project)
    logger.info("Created project '%s' at %s", name, project_dir)
    return project


def save_project(
    project: TimelineProject,
    *,
    expected_modified_at: str | None = None,
    bump_modified_at: bool = True,
    notify: bool = True,
) -> None:
    project_file = os.path.join(project.project_dir, "project.json")
    if expected_modified_at is None:
        expected_modified_at = getattr(project, "_expected_modified_at", None) or None
    # Serialize the read-version-check → tmp-write → atomic_replace as one critical
    # section per project (see _project_write_lock). Raising ProjectVersionConflict
    # releases the lock via the context manager. Diag + notify hooks run OUTSIDE the
    # lock (below) — they broadcast / schedule cross-thread work and must not be held.
    with _project_write_lock(project.project_dir):
        current_data = None
        if expected_modified_at:
            if os.path.isfile(project_file):
                with open(project_file, "r", encoding="utf-8") as f:
                    current_data = json.load(f)
                actual_modified_at = str(current_data.get("modified_at", "") or "")
            else:
                actual_modified_at = ""
            if actual_modified_at != expected_modified_at:
                raise ProjectVersionConflict(
                    project_dir=project.project_dir,
                    expected_modified_at=expected_modified_at,
                    actual_modified_at=actual_modified_at,
                    current_data=current_data,
                )

        if bump_modified_at:
            project.modified_at = datetime.now().isoformat()
        data = project.to_dict()
        tmp_file = f"{project_file}.{uuid.uuid4().hex}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        atomic_replace(tmp_file, project_file)
        if hasattr(project, "_expected_modified_at"):
            setattr(project, "_expected_modified_at", getattr(project, "modified_at", ""))
    # #36 diagnostic: every save with the caller's immediate stack frame so the diag ring
    # shows WHO bumped modified_at. Pairs with `project_version_conflict_409` events to
    # trace concurrent writers. Lazy import avoids circular dependency at module load.
    try:
        from .session_registry import record_diag_event as _record_diag_event
        import sys as _sys
        _caller = _sys._getframe(1)
        _caller_info = f"{os.path.basename(_caller.f_code.co_filename)}:{_caller.f_lineno} {_caller.f_code.co_name}"
        _canonical_project_id = str(getattr(project, "project_id", "") or "")
        _project_dir = str(getattr(project, "project_dir", "") or "")
        _folder_project_id = os.path.basename(os.path.normpath(_project_dir)) if _project_dir else ""
        _record_diag_event(
            "project_saved",
            project_id=_folder_project_id or _canonical_project_id,
            canonical_project_id=_canonical_project_id,
            modified_at=str(getattr(project, "modified_at", "") or ""),
            bumped=bool(bump_modified_at),
            caller=_caller_info,
        )
    except Exception:
        logger.debug("save_project failed to emit diag event", exc_info=True)
    if notify:
        for hook in list(_PROJECT_SAVED_HOOKS):
            try:
                hook(project)
            except Exception:
                logger.exception("Project save hook failed")


def load_project(project_dir: str) -> TimelineProject:
    project_file = os.path.join(project_dir, "project.json")
    if not os.path.isfile(project_file):
        raise FileNotFoundError(f"No project.json found in {project_dir}")

    with open(project_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    project = TimelineProject.from_dict(data, project_dir=project_dir)

    # Ensure subdirectories exist (in case of manual moves)
    for subdir in PROJECT_SUBDIRS:
        os.makedirs(os.path.join(project_dir, subdir), exist_ok=True)

    logger.debug("Loaded project '%s' from %s", project.name, project_dir)
    return project


def list_projects(base_dir: str) -> list[dict]:
    results = []
    if not os.path.isdir(base_dir):
        return results

    base_real = os.path.realpath(base_dir)
    trust_links = external_links.is_enabled()
    for entry in sorted(os.listdir(base_real)):
        entry_path = os.path.join(base_real, entry)
        linked = external_links.is_reparse_child(base_real, entry)
        if linked and not trust_links:
            logger.warning("Skipping linked project while external links are disabled: %s", entry_path)
            continue
        # Trust-on paths retain the scan-root anchor plus the lexical entry name. A
        # realpath here would turn a junction-backed project into its external target
        # and break the single-root resolver family used by routes and nodes.
        resolved_entry = os.path.abspath(entry_path) if trust_links else os.path.realpath(entry_path)
        if not path_within(base_real, resolved_entry):
            logger.warning("Skipping project entry outside base directory: %s", entry_path)
            continue
        project_file = os.path.join(resolved_entry, "project.json")
        if os.path.isdir(resolved_entry) and os.path.isfile(project_file):
            try:
                with open(project_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                scenes = data.get("scenes", [])
                assets = data.get("assets", [])
                # Backward compat: count clips from old flat format or from scenes
                clip_count = len(data.get("clips", []))
                if not clip_count:
                    clip_count = sum(len(s.get("clips", [])) for s in scenes)
                results.append({
                    "project_id": data.get("project_id", ""),
                    "name": data.get("name", entry),
                    "path": resolved_entry,
                    "linked": linked,
                    "fps": data.get("fps", 24.0),
                    "resolution": data.get("resolution", [1280, 720]),
                    "scene_count": len(scenes),
                    "clip_count": clip_count,
                    "asset_count": len(assets),
                    "modified_at": data.get("modified_at", ""),
                })
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Skipping invalid project at %s: %s", entry_path, e)
    return results


def _safe_dirname(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in name)
    return safe.strip().replace(" ", "-") or "untitled"
