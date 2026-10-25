"""Routes for run registration and observed run outputs (Output Views plumbing).

Canvas and mounted hosts use the project routes here; the Sonder App uses the
twins in ``sdk_routes`` (session secret), which share the response builders.
Reads are driven by ``outputs_changed`` hints and host polling, never by project
edits, so they sit outside the change-certificate contract. Media is never served
here: entries name files ComfyUI's ``/view`` serves.

Every read checks a file's presence on a worker thread and reports a held file
whose size or modification time changed as unavailable, never the new bytes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Iterable

from aiohttp import web

from . import run_control, run_outputs, sdk_runs, session_registry

logger = logging.getLogger("sonder_editor")

MAX_ID_CHARS = 200
MAX_EDITORS = 16


def _clean_id(value: Any) -> str:
    value = str(value or "").strip() if isinstance(value, (str, int)) and not isinstance(value, bool) else ""
    return value if value and len(value) <= MAX_ID_CHARS and "\x00" not in value else ""


def _file_path(file: dict) -> str | None:
    """The file a ``/view`` descriptor names, contained the way ``/view`` contains it."""
    try:
        import folder_paths
        base = folder_paths.get_directory_by_type(file["type"])
    except Exception:
        return None
    if not base:
        return None
    base = os.path.abspath(base)
    path = os.path.abspath(os.path.join(base, file.get("subfolder") or "", file["filename"]))
    try:
        if os.path.commonpath([base, path]) != base:
            return None
    except ValueError:
        return None
    return path


def file_stat(file: dict | None) -> tuple[int, int] | None:
    """``(size, mtime_ns)`` of an entry's file, or None when it is gone. Blocking."""
    if not isinstance(file, dict):
        return None
    path = _file_path(file)
    if not path:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns) if os.path.isfile(path) else None


def _revision(stat: tuple | None) -> str:
    """``size:mtime_ns`` as text (a nanosecond time is past 2^53 for JavaScript):
    a browser cache keyed by file and revision never shows changed bytes as
    the old file, and reuses a blob a cached rerun names again."""
    return f"{stat[0]}:{stat[1]}" if stat else ""


def _mark_available(entries: Iterable[dict]) -> None:
    for entry in entries:
        stat = file_stat(entry.get("file")) if entry.get("file") is not None else None
        entry["available"] = stat is not None
        entry["revision"] = _revision(stat)


def _reap_removed_runs() -> None:
    """End bound runs deleted or cleared from ComfyUI's queue before they started. Blocking."""
    run_outputs.reap_removed(run_outputs.queued_prompt_ids())


def _build_outputs(owners: list, workflow_id: str, sources: list[str], keep_holds: list[str] | None = None) -> dict:
    _reap_removed_runs()
    answer = run_outputs.read_outputs(owners, workflow_id, sources)
    for item in answer["sources"].values():
        if not item["result"]:
            continue
        entries = item["result"]["entries"]
        _mark_available(entries)
        if item["state"] != "ok":
            continue
        # A published result that cannot be shown says why, instead of "ok".
        if all(entry.get("file") is None for entry in entries):
            item["state"] = "unsupported"
        elif not any(entry["available"] for entry in entries):
            item["state"] = "missing_file"
    holds = []
    for hold in run_outputs.holds_for(owners, keep_holds):
        stat = file_stat(hold["entry"].get("file"))
        same = stat is not None and tuple(stat) == tuple(hold["stat"])
        holds.append({
            "hold_id": hold["hold_id"], "prompt_id": hold["prompt_id"], "source": hold["source"],
            "index": hold["index"], "entry": {**hold["entry"], "revision": _revision(hold["stat"])}, "run": hold["run"],
            "editor_context": hold.get("editor_context"), "fresh": hold.get("fresh"),
            "state": "ok" if same else "unavailable",
        })
    answer["holds"] = holds
    return {"ok": True, **answer}


def _history_entry(prompt_id: str) -> dict | None:
    try:
        from server import PromptServer
        history = PromptServer.instance.prompt_queue.get_history(prompt_id=prompt_id)
    except Exception:
        return None
    entry = history.get(prompt_id) if isinstance(history, dict) else None
    return entry if isinstance(entry, dict) else None


def _build_run(owners: list, prompt_id: str, workflow_id: str = "") -> dict | None:
    _reap_removed_runs()
    found = run_outputs.read_run(owners, prompt_id, workflow_id)
    if found is None:
        stone = run_outputs.tombstone_for(owners, prompt_id, workflow_id)
        entry = _history_entry(prompt_id) if stone else None
        if entry is None:
            return None
        found = run_outputs.entries_from_history(entry, stone)
    for source in found["sources"].values():
        _mark_available(source["entries"])
    return {"ok": True, **found}


def _sources(value: str) -> list[str]:
    return [item for item in (part.strip() for part in str(value or "").split(",")) if item][:run_outputs.MAX_SOURCES_PER_READ]


def _hold_ids(value: Any) -> list[str] | None:
    """The holds a page still shows (``keep_holds``); None when it names none at all."""
    if value is None:
        return None
    items = value if isinstance(value, list) else str(value).split(",")
    return [_clean_id(item) for item in items if _clean_id(item)][:run_outputs.MAX_HOLDS_PER_OWNER * 4]


async def outputs_response(owners: list, workflow_id: str, sources: str, keep_holds: Any = None) -> web.Response:
    return web.json_response(await asyncio.to_thread(_build_outputs, owners, str(workflow_id or ""), _sources(sources),
                                                     _hold_ids(keep_holds)))


async def run_response(owners: list, prompt_id: str, workflow_id: str = "") -> web.Response:
    found = await asyncio.to_thread(_build_run, owners, str(prompt_id or ""), str(workflow_id or ""))
    if found is None:
        return web.json_response({"ok": False, "code": "unknown_run"}, status=404)
    return web.json_response(found)


async def hold_response(owners: list, owner: tuple, body: dict) -> web.Response:
    prompt_id, source, index = str(body.get("prompt_id") or ""), str(body.get("source") or ""), body.get("index")
    found = await asyncio.to_thread(run_outputs.read_run, owners, prompt_id)
    entries = ((found or {}).get("sources", {}).get(source) or {}).get("entries") or []
    entry = entries[index] if isinstance(index, int) and not isinstance(index, bool) and 0 <= index < len(entries) else None
    stat = await asyncio.to_thread(file_stat, (entry or {}).get("file")) if entry else None
    code, hold_id = await asyncio.to_thread(run_outputs.hold, owners, owner, prompt_id, source, index, stat)
    if code != "ok":
        return web.json_response({"ok": False, "code": code}, status=409 if code == "too_many" else 404)
    return web.json_response({"ok": True, "hold_id": hold_id})


async def cancel_response(owners: list, prompt_id: str) -> web.Response:
    prompt_id = str(prompt_id or "")
    owned = any(bound["prompt_id"] == prompt_id for bound in await asyncio.to_thread(sdk_runs.visible_bound, owners))
    if not owned:
        return web.json_response({"ok": False, "code": "unknown_run"}, status=404)
    how = await asyncio.to_thread(run_control.cancel_prompt, prompt_id)
    if how == run_control.CANCEL_UNSUPPORTED:
        return web.json_response({"ok": False, "code": "unsupported"}, status=409)
    logger.info("Run %s cancel requested by its owner: %s", prompt_id, how)
    return web.json_response({"ok": True, "cancel": how})


def release_response(owners: list, body: dict) -> web.Response:
    """Release one hold, or the page's holds at once (``hold_ids``, on pagehide)."""
    if isinstance(body.get("hold_ids"), list):
        released = sum(run_outputs.release(owners, hold_id) for hold_id in _hold_ids(body["hold_ids"]) or [])
        return web.json_response({"ok": True, "released": released})
    released = run_outputs.release(owners, str(body.get("hold_id") or ""))
    return web.json_response({"ok": released}, status=200 if released else 404)


def register_run_output_routes(routes) -> None:
    async def body_of(request: web.Request) -> dict:
        try:
            body = await request.json()
        except Exception:
            return {}
        return body if isinstance(body, dict) else {}

    async def gate(project_id: Any, host_id: Any, session_id: Any, *, canvas_only: bool = False):
        project_id, host_id, session_id = _clean_id(project_id), _clean_id(host_id), _clean_id(session_id)
        if not project_id or not host_id or not session_id:
            return "invalid_host", None
        code, owner = await session_registry.run_owner_gate(project_id, host_id, session_id, canvas_only=canvas_only)
        return code, (None if code else owner)

    def refused(code: str) -> web.Response:
        return web.json_response({"ok": False, "code": code}, status=403 if code in {"not_owner", "not_canvas_host"} else 409)

    @routes.post("/sonder-editor/runs/register")
    async def api_register_run(request: web.Request) -> web.Response:
        """The canvas page registers the receipt it stamped on its Editor nodes."""
        body = await body_of(request)
        code, owner = await gate(body.get("project_id"), body.get("host_id"), body.get("session_id"), canvas_only=True)
        if code:
            return refused(code)
        editors = [_clean_id(item) for item in (body.get("editors") or [])][:MAX_EDITORS] if isinstance(body.get("editors"), list) else []
        purpose = "branch" if body.get("purpose") == "branch" else "run"
        result, prompt_id = sdk_runs.register_receipt(str(body.get("receipt") or ""), owner, [e for e in editors if e], purpose)
        if result != sdk_runs.REGISTER_OK:
            return web.json_response({"ok": False, "code": result}, status=429 if result == "too_many" else 400)
        if prompt_id:
            run_outputs.announce(prompt_id)  # it was observed unowned until now
        return web.json_response({"ok": True, "prompt_id": prompt_id})

    @routes.get("/sonder-editor/project/{project_id}/runs/outputs")
    async def api_run_outputs(request: web.Request) -> web.Response:
        query = request.rel_url.query
        code, owner = await gate(request.match_info.get("project_id"), query.get("host_id"), query.get("session_id"))
        if code:
            return refused(code)
        return await outputs_response([owner], query.get("workflow_id", ""), query.get("sources", ""), query.get("keep_holds"))

    @routes.post("/sonder-editor/project/{project_id}/runs/hold")
    async def api_run_hold(request: web.Request) -> web.Response:
        body = await body_of(request)
        code, owner = await gate(request.match_info.get("project_id"), body.get("host_id"), body.get("session_id"))
        if code:
            return refused(code)
        return await hold_response([owner], owner, body)

    @routes.post("/sonder-editor/project/{project_id}/runs/release")
    async def api_run_release(request: web.Request) -> web.Response:
        body = await body_of(request)
        code, owner = await gate(request.match_info.get("project_id"), body.get("host_id"), body.get("session_id"))
        if code:
            return refused(code)
        return release_response([owner], body)

    @routes.get("/sonder-editor/runs/capability")
    async def api_run_capability(_request: web.Request) -> web.Response:
        """Whether this ComfyUI can run one output branch (``partial_execution_targets``)."""
        return web.json_response({"ok": True, "branch": await asyncio.to_thread(run_control.branch_capability)})

    # Mounted run lane: the owner tab posts, its canvas host claims and reports.
    @routes.post("/sonder-editor/project/{project_id}/runs/requests")
    async def api_run_request(request: web.Request) -> web.Response:
        body = await body_of(request)
        project_id, host_id, session_id = (_clean_id(request.match_info.get("project_id")), _clean_id(body.get("host_id")),
                                           _clean_id(body.get("session_id")))
        result = await session_registry.post_run_request(project_id, host_id, session_id,
                                                         body.get("request_id"), body.get("target"))
        return web.json_response(result, status=200 if result.get("ok") else (
            403 if result.get("code") in {"not_owner", "not_canvas_host"} else 409))

    @routes.post("/sonder-editor/project/{project_id}/runs/requests/claim")
    async def api_run_request_claim(request: web.Request) -> web.Response:
        body = await body_of(request)
        result = await session_registry.claim_run_requests(
            _clean_id(request.match_info.get("project_id")), _clean_id(body.get("host_id")), _clean_id(body.get("session_id")))
        return web.json_response(result, status=200 if result.get("ok") else 403)

    @routes.post("/sonder-editor/project/{project_id}/runs/requests/{request_id}/report")
    async def api_run_request_report(request: web.Request) -> web.Response:
        body = await body_of(request)
        result = await session_registry.report_run_request(
            _clean_id(request.match_info.get("project_id")), _clean_id(body.get("host_id")), _clean_id(body.get("session_id")),
            request.match_info.get("request_id", ""), body.get("outcome"))
        return web.json_response(result, status=200 if result.get("ok") else 409)

    @routes.post("/sonder-editor/project/{project_id}/runs/{prompt_id}/cancel")
    async def api_run_cancel(request: web.Request) -> web.Response:
        """Cancel a prompt this host owns: ComfyUI parity (any page can cancel any job)."""
        body = await body_of(request)
        code, owner = await gate(request.match_info.get("project_id"), body.get("host_id"), body.get("session_id"))
        if code:
            return refused(code)
        return await cancel_response([owner], request.match_info.get("prompt_id", ""))

    # Wildcard id after the named subresources above.
    @routes.get("/sonder-editor/project/{project_id}/runs/{prompt_id}")
    async def api_run(request: web.Request) -> web.Response:
        query = request.rel_url.query
        code, owner = await gate(request.match_info.get("project_id"), query.get("host_id"), query.get("session_id"))
        if code:
            return refused(code)
        return await run_response([owner], request.match_info.get("prompt_id", ""), query.get("workflow_id", ""))
