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

from . import external_links, launcher_state, launcher_thumbnails, project_storage, sdk_templates
from .project_manager import scan_projects
from .project_storage_lifecycle import run_project_io
from .request_locality import MACHINE_ONLY_MESSAGE, is_loopback_request, local_only_response

logger = logging.getLogger("sonder_editor")

MAX_BODY_BYTES = 16 * 1024


def _scene_for(project: dict, folder: str) -> dict:
    """The scene the editor opens on (``launcher_thumbnails.opening_scene``); ``{}`` for a
    project without scenes."""
    chosen = launcher_thumbnails.opening_scene(project.get("scenes") or [], launcher_thumbnails.editor_scene_id(folder))
    return {"id": chosen["scene_id"], "name": chosen["name"]} if chosen else {}


def workflow_order(state: dict, templates: list[dict]) -> list[str]:
    """The order of your workflows, then starters: favorites first, then last used first,
    then never-used ones by newest import. Starters keep their bundled order, favorites
    first. The Launcher's and the Sonder App dropdown's one authority, and the fallback
    for preselection below."""
    by_template = state["last_workflow"]["by_template"]
    favorites = set(state.get("favorites") or [])
    ranked = sorted((t for t in templates if not t.get("starter")), key=lambda t: (
        t["template_id"] not in favorites, -by_template.get(t["template_id"], 0.0),
        -(t.get("imported_at") or 0), t.get("name", "").lower()))
    ranked.extend(sorted((t for t in templates if t.get("starter")), key=lambda t: (
        t["template_id"] not in favorites, t.get("order", 0), t["template_id"])))
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
        scene = _scene_for(entry, folder)
        key = launcher_thumbnails.thumbnail_key(entry["modified_at"], scene.get("id", ""))
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
            "scene": scene,
            "thumbnail": launcher_thumbnails.worker().describe(folder, entry["path"], key),
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


MAX_THUMBNAIL_FOLDERS = 200
THUMBNAIL_CACHE_SECONDS = 31536000  # the key changes with the content, so a file never does


def _folders(values) -> list[str]:
    if not isinstance(values, list) or len(values) > MAX_THUMBNAIL_FOLDERS:
        raise launcher_state.LauncherStateError(f"folders must be a list of at most {MAX_THUMBNAIL_FOLDERS} names.")
    if not all(isinstance(value, str) for value in values):
        raise launcher_state.LauncherStateError("folders must be names.")
    return list(dict.fromkeys(values))


def register_launcher_routes(routes, *, projects_dir: Callable[[], str],
                             project_dir_for_folder: Callable[[str], str | None]) -> None:
    """``project_dir_for_folder`` is the project routes' resolver: a contained folder with a
    ``project.json``, following a linked folder only while external links are on."""
    launcher_thumbnails.configure(project_dir_for_folder)
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

    @routes.put("/sonder-editor/launcher/favorite")
    @handle
    async def api_launcher_favorite(request: web.Request) -> web.Response:
        body = await _json_body(request)
        favorite = body.get("favorite")
        if not isinstance(favorite, bool):
            raise launcher_state.LauncherStateError("favorite must be true or false.")
        state = await asyncio.to_thread(launcher_state.set_favorite, body.get("id"), favorite)
        return web.json_response({"ok": True, **launcher_state.public_state(state)})

    @routes.post("/sonder-editor/launcher/link-preview")
    @handle
    async def api_launcher_link_preview(request: web.Request) -> web.Response:
        """What Link would add, read only, refused exactly as Link refuses. Machine-only,
        like Link itself: it reports on any folder of the server's file system."""
        if not is_loopback_request(request):
            return local_only_response(MACHINE_ONLY_MESSAGE)
        body = await _json_body(request)
        try:
            preview = await asyncio.to_thread(external_links.preview_link_target, body.get("path", ""))
        except external_links.LinkError as exc:
            return web.json_response({"ok": False, "code": exc.code, "error": str(exc)}, status=exc.status)
        return web.json_response({"ok": True, "preview": preview, "links_enabled": external_links.is_enabled()})

    def resolve(folders: list[str]) -> list[tuple[str, str]]:
        resolved = []
        for folder in folders:
            project_dir = project_dir_for_folder(folder)
            if project_dir:
                resolved.append((project_dir, folder))
        return resolved

    @routes.post("/sonder-editor/launcher/thumbnails/request")
    @handle
    async def api_launcher_thumbnails_request(request: web.Request) -> web.Response:
        """Queue the tiles the page shows, in the order given (the page sends newest first)."""
        folders = _folders((await _json_body(request)).get("folders"))
        jobs = await asyncio.to_thread(resolve, folders)
        # Folders, not paths: the worker resolves each again when it runs.
        launcher_thumbnails.worker().request([folder for _project_dir, folder in jobs])
        return web.json_response({"ok": True, "queued": len(jobs)})

    @routes.post("/sonder-editor/launcher/thumbnails/status")
    @handle
    async def api_launcher_thumbnails(request: web.Request) -> web.Response:
        """Each folder's ``{state, key, shown}``. Directory listings and stats only. A POST
        so a long list of folder names never meets the request-line limit."""
        folders = _folders((await _json_body(request)).get("folders"))

        def describe():
            return {folder: launcher_thumbnails.worker().describe(folder, project_dir)
                    for project_dir, folder in resolve(folders)}

        return web.json_response({"ok": True, "thumbnails": await asyncio.to_thread(describe)})

    @routes.get("/sonder-editor/launcher/thumbnail/{folder}/{name}")
    @handle
    async def api_launcher_thumbnail_file(request: web.Request) -> web.StreamResponse:
        key, _, ext = request.match_info["name"].partition(".")

        def find():
            project_dir = project_dir_for_folder(request.match_info["folder"])
            return launcher_thumbnails.media_path(project_dir, key, ext) if project_dir else ""

        path = await asyncio.to_thread(find)
        if not path:
            return web.json_response({"ok": False, "code": "not_found", "error": "No such thumbnail."}, status=404)
        return web.FileResponse(path, headers={
            "Cache-Control": f"private, max-age={THUMBNAIL_CACHE_SECONDS}, immutable",
            "Content-Type": "video/mp4" if ext == "mp4" else "image/jpeg",
        })
