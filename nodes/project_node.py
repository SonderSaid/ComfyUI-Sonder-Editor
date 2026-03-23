import os
import folder_paths

from ..server.project_manager import create_project, load_project, save_project, list_projects

CREATE_NEW = "+ Create New"


def _get_projects_base_dir():
    return os.path.join(folder_paths.get_output_directory(), "ltx_projects")


def _list_project_choices():
    """Return dropdown choices: '+ Create New' followed by existing project dir names."""
    base = _get_projects_base_dir()
    entries = [CREATE_NEW]
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if os.path.isfile(os.path.join(base, name, "project.json")):
                entries.append(name)
    return entries


class LTXProjectLoader:
    """Creates a new project or loads an existing one from the ComfyUI output directory."""

    CATEGORY = "LTX-Editor/Project"
    RETURN_TYPES = ("LTX_PROJECT",)
    RETURN_NAMES = ("project",)
    OUTPUT_TOOLTIPS = ("The project object to wire into other LTX Editor nodes.",)
    FUNCTION = "load_project"
    DESCRIPTION = (
        "Select an existing project from the dropdown, or choose '+ Create New' "
        "to create one with the name/fps/resolution settings below. "
        "Projects are stored in output/ltx_projects/."
    )

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "project": (_list_project_choices(), {
                    "tooltip": "Pick an existing project or '+ Create New' to create one.",
                }),
                "project_name": ("STRING", {
                    "default": "My Project",
                    "tooltip": "Name for the new project. Only used when '+ Create New' is selected.",
                }),
                "fps": ("FLOAT", {
                    "default": 24.0, "min": 1.0, "max": 120.0, "step": 0.001,
                    "tooltip": "Frame rate. Only used when creating a new project.",
                }),
                "width": ("INT", {
                    "default": 768, "min": 64, "max": 4096, "step": 8,
                    "tooltip": "Video width in pixels. Only used when creating a new project.",
                }),
                "height": ("INT", {
                    "default": 512, "min": 64, "max": 4096, "step": 8,
                    "tooltip": "Video height in pixels. Only used when creating a new project.",
                }),
            },
        }

    @classmethod
    def IS_CHANGED(s, **kwargs):
        return float("nan")

    def load_project(self, project, project_name, fps, width, height):
        base_dir = _get_projects_base_dir()

        if project == CREATE_NEW:
            proj = create_project(
                name=project_name,
                fps=fps,
                width=int(width),
                height=int(height),
                base_dir=base_dir,
            )
        else:
            project_dir = os.path.join(base_dir, project)
            proj = load_project(project_dir)

        return (proj,)


class LTXProjectInfo:
    """Displays project properties directly on the node and passes them as outputs."""

    CATEGORY = "LTX-Editor/Project"
    RETURN_TYPES = ("STRING", "FLOAT", "INT", "INT", "INT", "INT", "INT", "FLOAT", "STRING")
    RETURN_NAMES = ("name", "fps", "width", "height", "scene_count", "clip_count", "asset_count", "duration_sec", "project_dir")
    OUTPUT_TOOLTIPS = (
        "Project name.",
        "Frames per second.",
        "Video width in pixels.",
        "Video height in pixels.",
        "Number of scenes in the project.",
        "Total number of clips across all scenes.",
        "Number of assets in the project registry.",
        "Total timeline duration in seconds.",
        "Absolute path to the project directory.",
    )
    OUTPUT_NODE = True
    FUNCTION = "get_info"
    DESCRIPTION = "Shows project details on the node and outputs them for downstream use."

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "project": ("LTX_PROJECT", {"tooltip": "The project to inspect."}),
            }
        }

    def get_info(self, project):
        w, h = project.resolution
        name = project.name
        fps = project.fps
        scene_count = len(project.scenes)
        clip_count = len(project.clips)
        asset_count = len(project.assets)
        duration = project.duration_seconds
        project_dir = project.project_dir

        # Asset breakdown
        videos = len(project.get_assets_by_type("video"))
        images = len(project.get_assets_by_type("image"))
        audios = len(project.get_assets_by_type("audio"))

        return {
            "ui": {
                "text": [
                    f"Project: {name}",
                    f"FPS: {fps} | Resolution: {w}x{h}",
                    f"Scenes: {scene_count} | Clips: {clip_count} | Duration: {duration:.1f}s",
                    f"Assets: {asset_count} (Videos: {videos}, Images: {images}, Audio: {audios})",
                    f"Dir: {project_dir}",
                ]
            },
            "result": (name, fps, w, h, scene_count, clip_count, asset_count, duration, project_dir),
        }


class LTXProjectSave:
    """Saves the current project state to disk."""

    CATEGORY = "LTX-Editor/Project"
    RETURN_TYPES = ("LTX_PROJECT",)
    RETURN_NAMES = ("project",)
    OUTPUT_TOOLTIPS = ("The saved project, passed through for chaining.",)
    OUTPUT_NODE = True
    FUNCTION = "save"
    DESCRIPTION = "Persists the current project state to project.json on disk."

    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "project": ("LTX_PROJECT", {"tooltip": "The project to save."}),
            }
        }

    def save(self, project):
        save_project(project)
        return {
            "ui": {
                "text": [f"Saved: {project.name}", f"Dir: {project.project_dir}"]
            },
            "result": (project,),
        }
