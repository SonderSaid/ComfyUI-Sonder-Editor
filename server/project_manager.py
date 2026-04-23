import json
import os
import logging

from .timeline_state import TimelineProject

logger = logging.getLogger("sonder_editor")

PROJECT_SUBDIRS = [
    "media",
    "renders",
    "exports",
    os.path.join("cache", "thumbnails"),
    os.path.join("cache", "waveforms"),
    os.path.join("cache", "bridge_out"),
]


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


def save_project(project: TimelineProject) -> None:
    project_file = os.path.join(project.project_dir, "project.json")
    data = project.to_dict()
    with open(project_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


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
