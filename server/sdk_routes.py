"""HTTP routes for the Sonder App (SDK mode): status, templates and SDK sessions.

Registered from ``routes.py`` under ``/sonder-editor/sdk/…``, so the editor's
Origin check and security headers apply. Templates and the editor position are
install-level files (``sdk_templates``), never project data, so these routes are
outside the change-certificate contract. Session calls carry the session secret
in ``X-Sonder-SDK-Session``; calls that change state also carry the tab's
registry session id in ``X-Sonder-Tab-Session`` and must come from the current
owner tab.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from typing import Any, Awaitable, Callable

from aiohttp import web

from . import sdk_proxy, sdk_templates
from .sdk_install import install_state
from .sdk_runner import SdkError, get_runner

logger = logging.getLogger("sonder_editor")

SECRET_HEADER = "X-Sonder-SDK-Session"
TAB_HEADER = "X-Sonder-Tab-Session"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_request(request: web.Request) -> bool:
    """A request made on this machine, to this machine, not relayed.

    The peer must be loopback, the ``Host`` header must name a loopback host (a
    DNS-rebound page reaches 127.0.0.1 under a foreign Host), and no forwarding
    header may be present (a reverse proxy on this machine makes every peer
    look local).
    """
    remote = str(request.remote or "")
    try:
        address = ipaddress.ip_address(remote)
    except ValueError:
        return False
    if getattr(address, "ipv4_mapped", None) is not None:
        address = address.ipv4_mapped
    if not address.is_loopback:
        return False
    if request.headers.get("Forwarded") or request.headers.get("X-Forwarded-For"):
        return False
    host = request.headers.get("Host", "")
    if host.startswith("["):
        host_name = host[1:].split("]", 1)[0]
    else:
        host_name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host_name.lower() in _LOOPBACK_HOSTS


async def _read_capped(request: web.Request, limit: int) -> bytes:
    if request.content_length is not None and request.content_length > limit:
        raise sdk_templates.TemplateError("This workflow file is too large.", "too_large", 413)
    chunks, total = [], 0
    async for chunk in request.content.iter_chunked(1024 * 1024):
        total += len(chunk)
        if total > limit:
            raise sdk_templates.TemplateError("This workflow file is too large.", "too_large", 413)
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _template_error(exc: sdk_templates.TemplateError) -> web.Response:
    return web.json_response({"ok": False, "code": exc.code, "error": str(exc), **exc.details}, status=exc.status)


def _sdk_error(exc: SdkError) -> web.Response:
    return web.json_response(exc.payload(), status=exc.status)


def register_sdk_routes(
    routes,
    *,
    resolve_project: Callable[[str], Awaitable[tuple[str, list]]],
    remember_event_loop: Callable[[], None],
) -> None:
    runner = get_runner(resolve_project=resolve_project)

    def handle(func):
        async def wrapper(request: web.Request) -> web.StreamResponse:
            remember_event_loop()
            try:
                return await func(request)
            except sdk_templates.TemplateError as exc:
                return _template_error(exc)
            except SdkError as exc:
                return _sdk_error(exc)
            except Exception:
                logger.exception("Sonder App route %s failed", request.path)
                return web.json_response({"ok": False, "code": "internal", "error": "Something went wrong."}, status=500)

        wrapper.__name__ = func.__name__
        return wrapper

    # ── Launcher page ────────────────────────────────────────────────────────

    @routes.get("/sonder-editor/app")
    async def api_sdk_launcher(request: web.Request) -> web.StreamResponse:
        page = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "web", "sdk_app.html"))
        if not os.path.isfile(page):
            return web.json_response({"ok": False, "error": "Launcher page not found"}, status=404)
        return web.FileResponse(page)

    # ── Status ───────────────────────────────────────────────────────────────

    @routes.get("/sonder-editor/sdk/status")
    @handle
    async def api_sdk_status(request: web.Request) -> web.Response:
        install = await asyncio.to_thread(install_state)
        proxy = await asyncio.to_thread(sdk_proxy.proxy_status)
        return web.json_response({
            "ok": True,
            "install": install,
            "proxy": proxy,
            "can_manage": is_loopback_request(request),
            "launcher_url": "/sonder-editor/app",
        })

    # ── Templates ────────────────────────────────────────────────────────────

    @routes.get("/sonder-editor/sdk/templates")
    @handle
    async def api_sdk_list_templates(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "templates": await asyncio.to_thread(sdk_templates.list_templates)})

    @routes.post("/sonder-editor/sdk/templates")
    @handle
    async def api_sdk_import_template(request: web.Request) -> web.Response:
        raw = await _read_capped(request, sdk_templates.MAX_TEMPLATE_BYTES)
        template = await asyncio.to_thread(
            sdk_templates.import_template, raw,
            source_name=request.query.get("source_name", ""), name=request.query.get("name", ""),
        )
        return web.json_response({"ok": True, "template": template}, status=201)

    @routes.put("/sonder-editor/sdk/templates/{template_id}")
    @handle
    async def api_sdk_replace_template(request: web.Request) -> web.Response:
        template_id = sdk_templates.check_template_id(request.match_info.get("template_id"))
        raw = await _read_capped(request, sdk_templates.MAX_TEMPLATE_BYTES)
        async with runner.template_lock(template_id):  # never mid-Generate
            template = await asyncio.to_thread(
                sdk_templates.replace_template, template_id, raw, source_name=request.query.get("source_name", ""),
            )
        return web.json_response({"ok": True, "template": template})

    @routes.patch("/sonder-editor/sdk/templates/{template_id}")
    @handle
    async def api_sdk_rename_template(request: web.Request) -> web.Response:
        template_id = sdk_templates.check_template_id(request.match_info.get("template_id"))
        body = await _json_body(request)
        template = await asyncio.to_thread(sdk_templates.rename_template, template_id, body.get("name"))
        return web.json_response({"ok": True, "template": template})

    @routes.delete("/sonder-editor/sdk/templates/{template_id}")
    @handle
    async def api_sdk_delete_template(request: web.Request) -> web.Response:
        template_id = sdk_templates.check_template_id(request.match_info.get("template_id"))
        async with runner.template_lock(template_id):  # never mid-Generate
            await asyncio.to_thread(sdk_templates.delete_template, template_id)
        return web.json_response({"ok": True})

    # ── Sessions ─────────────────────────────────────────────────────────────

    def _secret(request: web.Request) -> str:
        return request.headers.get(SECRET_HEADER, "")

    def _tab(request: web.Request) -> str:
        return request.headers.get(TAB_HEADER, "")

    @routes.post("/sonder-editor/sdk/session/open")
    @handle
    async def api_sdk_open_session(request: web.Request) -> web.Response:
        body = await _json_body(request)
        result = await runner.open_session(str(body.get("template_id") or ""), str(body.get("project") or ""))
        return web.json_response(result)

    @routes.get("/sonder-editor/sdk/session/{session_id}/state")
    @handle
    async def api_sdk_session_state(request: web.Request) -> web.Response:
        session = runner.session_for(request.match_info.get("session_id", ""), _secret(request))
        values = await asyncio.to_thread(sdk_templates.read_editor_state, session.project)
        return web.json_response({
            "ok": True,
            "session_id": session.session_id,
            "project": session.project,
            "template_id": session.template_id,
            "template_name": runner.template_name(session),
            "values": values,
        })

    @routes.get("/sonder-editor/sdk/session/{session_id}/status")
    @handle
    async def api_sdk_session_status(request: web.Request) -> web.Response:
        session = runner.session_for(request.match_info.get("session_id", ""), _secret(request))
        return web.json_response(runner.status(session))

    @routes.put("/sonder-editor/sdk/session/{session_id}/values")
    @handle
    async def api_sdk_session_values(request: web.Request) -> web.Response:
        session = await runner.authorized(request.match_info.get("session_id", ""), _secret(request), _tab(request))
        body = await _json_body(request)
        values = await asyncio.to_thread(sdk_templates.write_editor_state, session.project, body.get("values"))
        return web.json_response({"ok": True, "values": values})

    @routes.post("/sonder-editor/sdk/session/{session_id}/generate")
    @handle
    async def api_sdk_generate(request: web.Request) -> web.Response:
        session = await runner.authorized(request.match_info.get("session_id", ""), _secret(request), _tab(request))
        body = await _json_body(request)
        result = await runner.generate(
            session,
            count=body.get("count", 1),
            editor_values=body.get("editor_values"),
            render_cache_max_bytes=body.get("render_cache_max_bytes", 0),
        )
        return web.json_response(result, status=202 if result.get("run_ids") else 502)

    @routes.post("/sonder-editor/sdk/session/{session_id}/cancel")
    @handle
    async def api_sdk_cancel(request: web.Request) -> web.Response:
        session = await runner.authorized(request.match_info.get("session_id", ""), _secret(request), _tab(request))
        return web.json_response(await runner.cancel(session))
