"""HTTP routes the Launcher page reads and writes: projects, hidden items, last workflows.

Registered from ``routes.py`` beside the SDK routes, under ``/sonder-editor/launcher/…``,
so the editor's Origin check and security headers apply. These reads run when the
Launcher loads, when it comes back from the editor and on Check again, never because
a project changed, so they are outside the change-certificate contract. What they
remember is install-level (``launcher_state``), never project data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable

from aiohttp import web

from . import launcher_state, project_storage, sdk_templates
from .project_manager import scan_projects
from .project_storage_lifecycle import run_project_io
from .request_locality import is_loopback_request

logger = logging.getLogger("sonder_editor")

MAX_BODY_BYTES = 16 * 1024


def _scene_for(project: dict, editor_scene_id: str) -> dict:
    """The scene the editor opens on: the SDK editor's last one if it still exists,
    else the first by order. ``{}`` for a project without scenes."""
    scenes = project.get("scenes") or []
    if not scenes:
        return {}
    chosen = next((scene for scene in scenes if editor_scene_id and scene["scene_id"] == editor_scene_id), None)
    if chosen is None:
        chosen = min(scenes, key=lambda scene: scene["order"])
    return {"id": chosen["scene_id"], "name": chosen["name"]}


def _editor_scene_id(folder: str) -> str:
    try:
        return str(sdk_templates.read_editor_state(folder).get("scene_id") or "")
    except Exception:  # an unreadable position only loses the preselected scene
        logger.debug("SDK editor state for %s unreadable", folder, exc_info=True)
        return ""


def workflow_order(state: dict, templates: list[dict]) -> list[str]:
    """The sidebar's order for your workflows: last used first, then never-used ones by
    newest import. The Launcher's one authority for it, and for the fallback below."""
    by_template = state["last_workflow"]["by_template"]
    ranked = sorted(templates, key=lambda t: (-by_template.get(t["template_id"], 0.0),
                                              -(t.get("imported_at") or 0), t.get("name", "").lower()))
    return [t["template_id"] for t in ranked]


def _templates() -> list[dict]:
    try:
        return sdk_templates.list_templates()
    except sdk_templates.TemplateError:
        return []  # no user directory: no workflows to preselect


def launcher_projects(base_dir: str) -> dict:
    """The Launcher's project payload. Runs on the project I/O pool (it takes project locks)."""
    entries, unreadable, linked_hidden = scan_projects(base_dir, detail=True)
    state = launcher_state.read_state()
    hidden = set(state["hidden"]["projects"])
    by_project = state["last_workflow"]["by_project"]
    order = workflow_order(state, _templates())
    projects = []
    for entry in entries:
        folder = entry["folder"]
        projects.append({
            "folder": folder,
            "project_id": entry["project_id"],
            "name": entry["name"],
            "linked": entry["linked"],
            "fps": entry["fps"],
            "resolution": entry["resolution"],
            "scene_count": entry["scene_count"],
            "modified_at": entry["modified_at"],
            "format_version": entry["format_version"],
            "written_with": entry["written_with"],
            "too_new": entry["too_new"],
            "upgrade": entry["upgrade"],
            "scene": _scene_for(entry, _editor_scene_id(folder)),
            "hidden": folder in hidden,
            "last_workflow": by_project.get(folder, {}).get("template_id", ""),
            # What to preselect: its last workflow, else the last used anywhere, else
            # the top of the sidebar, skipping hidden and deleted ones ("" when none).
            "workflow": launcher_state.preferred_workflow(state, folder, order),
        })
    return {
        "ok": True,
        "projects": projects,
        "workflow_order": order,
        "unreadable": [{**item, "hidden": item["folder"] in hidden} for item in unreadable],
        "linked_hidden_count": linked_hidden,
        "current_format": project_storage.CURRENT_FORMAT_VERSION,
        "readable_formats": list(project_storage.READABLE_FORMAT_VERSIONS),
        "format_first_release": {str(k): v for k, v in project_storage.FORMAT_FIRST_RELEASE.items()},
    }


class BodyTooLarge(ValueError):
    pass


async def _json_body(request: web.Request) -> dict:
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        raise BodyTooLarge("Request too large.")
    chunks, total = [], 0
    async for chunk in request.content.iter_chunked(4096):
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise BodyTooLarge("Request too large.")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        body = json.loads(raw.decode("utf-8") or "{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise launcher_state.LauncherStateError("Invalid JSON body.") from exc
    if not isinstance(body, dict):
        raise launcher_state.LauncherStateError("JSON object required.")
    return body


def register_launcher_routes(routes, *, projects_dir: Callable[[], str]) -> None:
    def handle(func):
        async def wrapper(request: web.Request) -> web.StreamResponse:
            try:
                return await func(request)
            except BodyTooLarge as exc:
                return web.json_response({"ok": False, "code": "too_large", "error": str(exc)}, status=413)
            except launcher_state.LauncherStateError as exc:
                return web.json_response({"ok": False, "code": "invalid", "error": str(exc)}, status=400)
            except launcher_state.LauncherStateUnavailable as exc:
                return web.json_response({"ok": False, "code": "storage_unavailable", "error": str(exc)},
                                         status=503)
            except Exception:
                logger.exception("Launcher route %s failed", request.path)
                return web.json_response({"ok": False, "code": "internal", "error": "Something went wrong."},
                                         status=500)

        wrapper.__name__ = func.__name__
        return wrapper

    @routes.get("/sonder-editor/launcher/projects")
    @handle
    async def api_launcher_projects(request: web.Request) -> web.Response:
        base_dir = projects_dir()
        if not base_dir:
            return web.json_response({"ok": False, "code": "projects_unavailable",
                                      "error": "The projects folder is unavailable."}, status=500)
        payload = await run_project_io(launcher_projects, base_dir)
        payload["can_manage"] = is_loopback_request(request)
        return web.json_response(payload)

    @routes.get("/sonder-editor/launcher/state")
    @handle
    async def api_launcher_state(request: web.Request) -> web.Response:
        state = await asyncio.to_thread(launcher_state.read_state)
        return web.json_response({"ok": True, **launcher_state.public_state(state)})

    @routes.put("/sonder-editor/launcher/hidden")
    @handle
    async def api_launcher_hidden(request: web.Request) -> web.Response:
        body = await _json_body(request)
        hidden = body.get("hidden")
        if not isinstance(hidden, bool):
            raise launcher_state.LauncherStateError("hidden must be true or false.")
        state = await asyncio.to_thread(launcher_state.set_hidden, str(body.get("kind") or ""), body.get("id"), hidden)
        return web.json_response({"ok": True, **launcher_state.public_state(state)})
