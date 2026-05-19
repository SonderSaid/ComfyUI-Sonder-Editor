import json
import os
import logging
import uuid
from datetime import datetime
from typing import Callable

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
    width: int = 768,
    height: int = 512,
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
    os.replace(tmp_file, project_file)
    if hasattr(project, "_expected_modified_at"):
        setattr(project, "_expected_modified_at", getattr(project, "modified_at", ""))
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

    logger.info("Loaded project '%s' from %s", project.name, project_dir)
    return project


def list_projects(base_dir: str) -> list[dict]:
    results = []
    if not os.path.isdir(base_dir):
        return results

    for entry in sorted(os.listdir(base_dir)):
        entry_path = os.path.join(base_dir, entry)
        project_file = os.path.join(entry_path, "project.json")
        if os.path.isdir(entry_path) and os.path.isfile(project_file):
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
                    "path": entry_path,
                    "fps": data.get("fps", 24.0),
                    "resolution": data.get("resolution", [768, 512]),
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
