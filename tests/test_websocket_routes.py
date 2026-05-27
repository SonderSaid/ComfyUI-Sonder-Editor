import asyncio
import importlib
import os
import sys
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server
import server.routes as routes


def _load_route_module(monkeypatch):
    fake_prompt_server = SimpleNamespace(instance=SimpleNamespace(routes=web.RouteTableDef()))
    monkeypatch.setattr(server, "PromptServer", fake_prompt_server, raising=False)
    return importlib.reload(routes)


def _route_handler(route_module, method, path):
    for route in route_module.routes:
        if route.method == method and route.path == path:
            return route.handler
    raise AssertionError(f"Route not found: {method} {path}")


def test_websocket_sends_swarm_sid_status_before_subscription(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    handler = _route_handler(route_module, "GET", "/sonder-editor/ws")

    async def exercise():
        app = web.Application()
        app.router.add_get("/sonder-editor/ws", handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            ws = await client.ws_connect("/sonder-editor/ws?project_id=project-1")
            first = await ws.receive_json(timeout=1)
            second = await ws.receive_json(timeout=1)
            await ws.close()
            return first, second
        finally:
            await client.close()

    first, second = asyncio.run(exercise())

    assert first["type"] == "status"
    assert isinstance(first.get("data"), dict)
    assert isinstance(first["data"].get("sid"), str)
    assert len(first["data"]["sid"]) == 32
    assert second == {"type": "subscribed", "project_id": "project-1"}
