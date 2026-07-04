import asyncio
import importlib
import os
import sys
from types import SimpleNamespace

from aiohttp import web
from aiohttp.client_exceptions import WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
import pytest

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


def _app_with_websocket_handler(handler):
    app = web.Application()
    app.router.add_get("/sonder-editor/ws", handler)
    return app


def test_websocket_sends_swarm_sid_status_before_subscription(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    handler = _route_handler(route_module, "GET", "/sonder-editor/ws")

    async def exercise():
        client = TestClient(TestServer(_app_with_websocket_handler(handler)))
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


def test_websocket_blocks_cross_origin_before_handshake(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    handler = _route_handler(route_module, "GET", "/sonder-editor/ws")

    async def exercise():
        client = TestClient(TestServer(_app_with_websocket_handler(handler)))
        await client.start_server()
        try:
            response = await client.get(
                "/sonder-editor/ws",
                headers={"Origin": "https://example.invalid"},
            )
            payload = await response.json()
            with pytest.raises(WSServerHandshakeError) as exc_info:
                await client.ws_connect(
                    "/sonder-editor/ws",
                    headers={"Origin": "https://example.invalid"},
                )
            return response.status, payload, response.headers, exc_info.value.status
        finally:
            await client.close()

    status, payload, headers, handshake_status = asyncio.run(exercise())

    assert status == 403
    assert payload["code"] == "cross_origin_blocked"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert handshake_status == 403


def test_websocket_allows_same_origin_header(monkeypatch):
    route_module = _load_route_module(monkeypatch)
    handler = _route_handler(route_module, "GET", "/sonder-editor/ws")

    async def exercise():
        client = TestClient(TestServer(_app_with_websocket_handler(handler)))
        await client.start_server()
        try:
            origin = str(client.make_url("/sonder-editor/ws")).split("/sonder-editor", 1)[0]
            ws = await client.ws_connect(
                "/sonder-editor/ws",
                headers={"Origin": origin},
            )
            first = await ws.receive_json(timeout=1)
            await ws.close()
            return first
        finally:
            await client.close()

    first = asyncio.run(exercise())

    assert first["type"] == "status"
    assert len(first["data"]["sid"]) == 32
