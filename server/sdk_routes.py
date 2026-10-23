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
import json
import logging
import os
from typing import Any, Awaitable, Callable

from aiohttp import web

from . import control_descriptors, launcher_state, model_requirements, run_output_routes, sdk_install, sdk_proxy, sdk_templates
from .request_locality import is_loopback_request, local_only_response
from .sdk_install import install_state
from .sdk_runner import SdkError, await_template_io, get_runner

logger = logging.getLogger("sonder_editor")

SECRET_HEADER = "X-Sonder-SDK-Session"
TAB_HEADER = "X-Sonder-Tab-Session"
UNINSTALL_DRAIN_SEC = 30.0


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


def _public_installer(installer: dict) -> dict:
    """The installer status for a browser on another machine: no commands or pip
    output, which carry ComfyUI's absolute interpreter path."""
    job = dict(installer.get("job") or {})
    if isinstance(job.get("result"), dict):
        job["result"] = {k: v for k, v in job["result"].items() if k not in ("commands", "detail")}
    interrupted = installer.get("interrupted")
    if isinstance(interrupted, dict):
        interrupted = {k: v for k, v in interrupted.items() if k != "commands"}
    return {**installer, "job": job, "interrupted": interrupted}


def _upstream_or_empty() -> str:
    try:
        return sdk_proxy.own_upstream_url()
    except sdk_proxy.ProxyUnavailable:
        return ""  # HTTPS ComfyUI: no shortcut


def _first_line(text: Any) -> str:
    return (str(text or "").strip().splitlines() or [""])[0].strip()[:200]


def _session_workflows(current: str) -> dict:
    """``{current, workflows: [{template_id, name, description, starter, ok, favorite}]}``."""
    from .launcher_routes import workflow_order

    try:
        templates = sdk_templates.list_templates()
    except sdk_templates.TemplateError:
        templates = []
    state = launcher_state.read_state()
    hidden, favorites = set(state["hidden"]["workflows"]), set(state["favorites"])
    by_id = {t["template_id"]: t for t in templates}
    order = [tid for tid in workflow_order(state, templates) if tid not in hidden or tid == current]
    order = [tid for tid in order if tid in favorites] + [tid for tid in order if tid not in favorites]
    workflows = []
    for tid in order:
        template = by_id[tid]
        validation = template.get("validation") or {}
        workflows.append({
            "template_id": tid,
            "name": template.get("name", ""),
            "description": _first_line(template.get("description")),
            "starter": bool(template.get("starter")),
            "ok": bool(validation.get("ok", True)) and not validation.get("blocking"),
            "missing_nodes": list(validation.get("missing_nodes") or []),
            "favorite": tid in favorites,
        })
    return {"ok": True, "current": current, "workflows": workflows}


def register_sdk_routes(
    routes,
    *,
    resolve_project: Callable[[str], Awaitable[tuple[str, list]]],
    remember_event_loop: Callable[[], None],
    pending_jobs: Callable[[str], Awaitable[int]] | None = None,
    projects_dir: Callable[[], str] = lambda: "",
    app=None,
) -> None:
    runner = get_runner(resolve_project=resolve_project)

    def write_shortcut() -> str:
        return sdk_install.write_shortcut(projects_dir(), _upstream_or_empty())

    def delete_shortcut() -> bool:
        return sdk_install.delete_shortcut(projects_dir())

    def proxy_running_elsewhere() -> bool:
        status = sdk_proxy.proxy_status()
        return bool(status.get("running")) and not status.get("managed")

    if app is not None:
        async def refresh_shortcut(_app=None) -> None:
            # The port may differ from the last run; only while both packages are present.
            try:
                await asyncio.to_thread(sdk_install.refresh_shortcut, projects_dir(), _upstream_or_empty())
            except Exception:
                logger.warning("Refreshing the Sonder Launcher shortcut failed", exc_info=True)

        app.on_startup.append(refresh_shortcut)

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
        installer = await asyncio.to_thread(sdk_install.installer_status)
        can_manage = is_loopback_request(request)
        return web.json_response({
            "ok": True,
            "install": install,
            "proxy": proxy,
            "installer": installer if can_manage else _public_installer(installer),
            "pinned": sdk_install.PINNED,
            "can_manage": can_manage,
            "launcher_url": "/sonder-editor/app",
        })

    # ── Install and uninstall (from the ComfyUI machine only) ───────────────

    def _local_only(request: web.Request) -> web.Response | None:
        if is_loopback_request(request):
            return None
        return local_only_response("Install from the ComfyUI machine.")

    def _refused(exc: sdk_install.InstallRefused) -> web.Response:
        status = 409 if exc.code in ("busy", "runs_active") else 400
        return web.json_response({"ok": False, "code": exc.code, "error": str(exc), **exc.details}, status=status)

    @routes.post("/sonder-editor/sdk/install")
    @handle
    async def api_sdk_install(request: web.Request) -> web.Response:
        refused = _local_only(request)
        if refused is not None:
            return refused
        try:
            job = sdk_install.start_install(shortcut=write_shortcut)
        except sdk_install.InstallRefused as exc:
            return _refused(exc)
        return web.json_response({"ok": True, "job": job}, status=202)

    @routes.post("/sonder-editor/sdk/uninstall")
    @handle
    async def api_sdk_uninstall(request: web.Request) -> web.Response:
        refused = _local_only(request)
        if refused is not None:
            return refused
        # Reserve first: from here new Generates and proxy starts are refused
        # (sdk_install.uninstall_running), so the checks below cannot go stale.
        try:
            previous = sdk_install.reserve_job("uninstall")
        except sdk_install.InstallRefused as exc:
            return _refused(exc)
        launched = False
        try:
            if not await runner.wait_idle(UNINSTALL_DRAIN_SEC) or runner.has_unfinished_runs():
                return _refused(sdk_install.InstallRefused(
                    "Takes are still generating. Wait for them or cancel them, then uninstall.", "runs_active"))
            await runner.aclose_clients()
            job = sdk_install.launch_job("uninstall", lambda: sdk_install.uninstall(
                stop_proxy=sdk_proxy.stop_owned_proxy,
                proxy_running_elsewhere=proxy_running_elsewhere,
                shortcut_delete=delete_shortcut,
            ))
            launched = True
        finally:
            if not launched:
                sdk_install.release_job(previous)
        return web.json_response({"ok": True, "job": job}, status=202)

    # ── Templates ────────────────────────────────────────────────────────────

    @routes.get("/sonder-editor/sdk/templates")
    @handle
    async def api_sdk_list_templates(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "templates": await asyncio.to_thread(sdk_templates.list_templates)})

    @routes.get("/sonder-editor/sdk/templates/{template_id}/requirements")
    @handle
    async def api_sdk_template_requirements(request: web.Request) -> web.Response:
        template_id = sdk_templates.check_template_id(request.match_info.get("template_id"))
        result = await asyncio.to_thread(sdk_templates.template_requirements, template_id)
        # Where each file goes on disk is shown only to a browser on the ComfyUI
        # machine, on a same-origin page; others see the models/... form.
        from .routes import _same_origin_request
        if is_loopback_request(request) and _same_origin_request(request):
            for item in result.get("missing_models", []):
                item["path"] = model_requirements.target_path(item)
        return web.json_response(result)

    @routes.get("/sonder-editor/sdk/templates/{template_id}/export")
    @handle
    async def api_sdk_export_template(request: web.Request) -> web.Response:
        # GET returns the whole workflow; the mutation-only middleware does not guard it.
        from .routes import _same_origin_request, _cross_origin_blocked_response
        if not _same_origin_request(request):
            return _cross_origin_blocked_response(request)
        template_id = sdk_templates.check_template_id(request.match_info.get("template_id"))
        raw = await asyncio.to_thread(sdk_templates.export_curated, template_id)
        return web.Response(body=raw, content_type="application/json", headers={"Cache-Control": "no-store"})

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
            template = await await_template_io(
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
            await await_template_io(sdk_templates.delete_template, template_id)
        return web.json_response({"ok": True})

    # ── Sessions ─────────────────────────────────────────────────────────────

    def _secret(request: web.Request) -> str:
        return request.headers.get(SECRET_HEADER, "")

    def _tab(request: web.Request) -> str:
        return request.headers.get(TAB_HEADER, "")

    def _manage_query(request: web.Request) -> tuple[bool, str]:
        """``?manage=1&filter=`` asks for Manage's candidates (only while it is open)."""
        query = request.query.get("filter", "")[:control_descriptors.FILTER_MAX_CHARS]
        return request.query.get("manage") == "1", query

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

    @routes.get("/sonder-editor/sdk/session/{session_id}/workflows")
    @handle
    async def api_sdk_session_workflows(request: web.Request) -> web.Response:
        """The Workflow menu: favorites first, then the Launcher's order; hidden workflows
        only when current."""
        session = runner.session_for(request.match_info.get("session_id", ""), _secret(request))
        current = session.template_id
        return web.json_response(await asyncio.to_thread(_session_workflows, current))

    @routes.post("/sonder-editor/sdk/session/{session_id}/workflow")
    @handle
    async def api_sdk_switch_workflow(request: web.Request) -> web.Response:
        body = await _json_body(request)
        result = await runner.switch_workflow(
            request.match_info.get("session_id", ""), _secret(request), _tab(request),
            body.get("template_id"), body.get("expected_template_id"))
        session = runner.session_for(request.match_info.get("session_id", ""), _secret(request))
        pending = 0
        if pending_jobs is not None:
            try:
                pending = await pending_jobs(session.project_dir)
            except Exception:
                logger.warning("Counting the queued jobs after a workflow switch failed", exc_info=True)
        return web.json_response({**result, "pending_jobs": pending})

    @routes.get("/sonder-editor/sdk/session/{session_id}/controls")
    @handle
    async def api_sdk_controls(request: web.Request) -> web.Response:
        session = runner.session_for(request.match_info.get("session_id", ""), _secret(request))
        manage, query = _manage_query(request)
        snapshot = await runner.controls_view(session, manage=manage, query=query)
        view = await asyncio.to_thread(control_descriptors.json_safe, snapshot["view"])
        return web.json_response({"ok": True, "view": view, "seq": snapshot["seq"], "template_id": snapshot["template_id"]})

    @routes.put("/sonder-editor/sdk/session/{session_id}/controls")
    @handle
    async def api_sdk_change_controls(request: web.Request) -> web.Response:
        session_id = request.match_info.get("session_id", "")
        await runner.authorized(session_id, _secret(request), _tab(request))
        raw = await _read_capped(request, 512 * 1024)
        try:
            body = await asyncio.to_thread(json.loads, raw)
        except (ValueError, UnicodeError):
            body = None
        manage, query = _manage_query(request)
        body = body if isinstance(body, dict) else {}
        snapshot = await runner.change_controls(
            session_id, _secret(request), _tab(request), body.get("changes"),
            manage=manage, query=query, expected_template_id=body.get("template_id"))
        view = await asyncio.to_thread(control_descriptors.json_safe, snapshot["view"])
        return web.json_response({"ok": True, "view": view, "seq": snapshot["seq"], "template_id": snapshot["template_id"]})

    @routes.get("/sonder-editor/sdk/session/{session_id}/status")
    @handle
    async def api_sdk_session_status(request: web.Request) -> web.Response:
        session = runner.session_for(request.match_info.get("session_id", ""), _secret(request))
        snapshot = runner.status(session)
        if _tab(request):
            await runner.authorized(session.session_id, _secret(request), _tab(request))
            snapshot["details"] = {run.run_id: run.detail for run in session.runs if run.detail}
        return web.json_response(snapshot)

    @routes.put("/sonder-editor/sdk/session/{session_id}/values")
    @handle
    async def api_sdk_session_values(request: web.Request) -> web.Response:
        session = await runner.authorized(request.match_info.get("session_id", ""), _secret(request), _tab(request))
        body = await _json_body(request)
        writer = sdk_templates.clean_editor_writer({"tab": _tab(request), "seq": body.get("seq")})
        result = await asyncio.to_thread(sdk_templates.write_editor_state, session.project, body.get("values"), writer=writer)
        return web.json_response({"ok": True, **result})

    @routes.post("/sonder-editor/sdk/session/{session_id}/generate")
    @handle
    async def api_sdk_generate(request: web.Request) -> web.Response:
        session = await runner.authorized(request.match_info.get("session_id", ""), _secret(request), _tab(request))
        body = await _json_body(request)
        result = await runner.generate(
            session,
            count=body.get("count", 1),
            editor_values=body.get("editor_values"),
            tab_session_id=_tab(request),
            editor_values_seq=body.get("editor_values_seq"),
            render_cache_max_bytes=body.get("render_cache_max_bytes", 0),
            expected_template_id=body.get("template_id"),
        )
        return web.json_response(result, status=202 if result.get("run_ids") else 502)

    # ── Observed run outputs (shared builders in run_output_routes) ──────────

    async def output_owners(request: web.Request) -> tuple[list, tuple]:
        """The session's runs as owners, resolved now: absorbed runs follow their session."""
        session = await runner.authorized(request.match_info.get("session_id", ""), _secret(request), _tab(request))
        holder = ("sdk_session", session.session_id)
        return [("sdk", run.run_id) for run in session.runs] + [holder], holder

    @routes.get("/sonder-editor/sdk/session/{session_id}/runs/outputs")
    @handle
    async def api_sdk_run_outputs(request: web.Request) -> web.Response:
        owners, _holder = await output_owners(request)
        query = request.rel_url.query
        return await run_output_routes.outputs_response(owners, query.get("workflow_id", ""), query.get("sources", ""))

    @routes.post("/sonder-editor/sdk/session/{session_id}/runs/hold")
    @handle
    async def api_sdk_run_hold(request: web.Request) -> web.Response:
        owners, holder = await output_owners(request)
        return await run_output_routes.hold_response(owners, holder, await _json_body(request))

    @routes.post("/sonder-editor/sdk/session/{session_id}/runs/release")
    @handle
    async def api_sdk_run_release(request: web.Request) -> web.Response:
        owners, _holder = await output_owners(request)
        return run_output_routes.release_response(owners, await _json_body(request))

    @routes.get("/sonder-editor/sdk/session/{session_id}/runs/{prompt_id}")
    @handle
    async def api_sdk_run(request: web.Request) -> web.Response:
        owners, _holder = await output_owners(request)
        return await run_output_routes.run_response(owners, request.match_info.get("prompt_id", ""),
                                                    request.rel_url.query.get("workflow_id", ""))

    @routes.post("/sonder-editor/sdk/session/{session_id}/cancel")
    @handle
    async def api_sdk_cancel(request: web.Request) -> web.Response:
        if not _tab(request):
            return web.json_response(await runner.cancel_by_secret(request.match_info.get("session_id", ""), _secret(request)))
        session = await runner.authorized(request.match_info.get("session_id", ""), _secret(request), _tab(request))
        return web.json_response(await runner.cancel(session))
