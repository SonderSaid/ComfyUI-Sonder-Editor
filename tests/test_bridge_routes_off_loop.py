"""The usage scanners must not run on the event loop.

`hydrate_job` takes the per-project write lock, and `_find_asset_usages` /
`_aggregate_asset_usages` reach it. An automatic component migration can hold that
lock for seconds, so a bare call in a coroutine body stalls the loop for the
remainder of it — the stall moving project I/O into workers was meant to remove.

Scope, stated because the scan below cannot enforce more than this: the lock is also
reachable through plain ATTRIBUTE ACCESS. `Asset.__getattribute__` intercepts
`generation_params` and calls `hydrate_asset`, which takes the same lock, so
`_asset_payload` on an unhydrated asset acquires it without naming any function a
static scan could match. That surface is tracked separately; do not read a green run
here as proof that the loop never touches the lock.
"""
import ast
import asyncio
import threading

import pytest

from test_project_mutation_pipeline_backend import (
    DummyRequest, _load_route_module, _route_handler)

BRIDGE_ROUTES = [
    "/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-guides",
    "/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-drivers",
    "/sonder-editor/project/{project_id}/scenes/{scene_id}/bridge-references",
    "/sonder-editor/project/{project_id}/scenes/{scene_id}/prompt-payload",
]

LOCK_TAKERS = {"hydrate_job", "_find_asset_usages", "_aggregate_asset_usages"}


class _BridgeRequest(DummyRequest):
    """The bridge routes emit entry diagnostics that read `rel_url`."""

    @property
    def rel_url(self):
        return self.path


class _LoaderReached(Exception):
    """Raised from the patched loader so the handler stops right after it runs."""


def test_every_bridge_route_resolves_its_scene_off_the_event_loop(monkeypatch):
    routes = _load_route_module(monkeypatch)
    threads = []

    def recording_loader(request, scene_id):
        threads.append(threading.get_ident())
        raise _LoaderReached()

    monkeypatch.setattr(routes, "_load_scene_for_bridge", recording_loader)

    async def run():
        loop_thread = threading.get_ident()
        for path in BRIDGE_ROUTES:
            handler = _route_handler(routes, "GET", path)
            with pytest.raises(_LoaderReached):
                await handler(_BridgeRequest(
                    match_info={"project_id": "project", "scene_id": "scene-1"},
                    method="GET", path=path))

        assert len(threads) == len(BRIDGE_ROUTES), (
            "a bridge route stopped going through the shared loader")
        assert all(thread != loop_thread for thread in threads), (
            "a bridge route resolved its scene on the event loop, where a migrating "
            "save would block it")

    asyncio.run(run())


def _on_loop_lock_takers(source: str):
    """Every lock-taking call whose nearest enclosing function is a coroutine.

    A call inside a plain `def` or a `lambda` is fine — those are what get handed to
    `asyncio.to_thread`. Only a call sitting directly in an `async def` body runs on
    the loop.
    """
    findings = []
    stack = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(False)
            self.generic_visit(node)
            stack.pop()

        def visit_AsyncFunctionDef(self, node):
            stack.append(True)
            self.generic_visit(node)
            stack.pop()

        def visit_Lambda(self, node):
            stack.append(False)
            self.generic_visit(node)
            stack.pop()

        def visit_Call(self, node):
            # Match `hydrate_job(...)` and `ps.hydrate_job(...)` alike: a
            # module-qualified call is the obvious way this would come back.
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in LOCK_TAKERS and stack and stack[-1]:
                findings.append((node.lineno, name))
            self.generic_visit(node)

    Visitor().visit(ast.parse(source))
    return findings


def test_no_route_runs_a_usage_scanner_on_the_event_loop(monkeypatch):
    routes = _load_route_module(monkeypatch)
    with open(routes.__file__, encoding="utf-8") as handle:
        findings = _on_loop_lock_takers(handle.read())

    assert findings == [], (
        "these scanners reach the per-project write lock and are called directly in a "
        "coroutine body; move them into the worker that loads the project: "
        + ", ".join(f"routes.py:{line} {name}" for line, name in findings))


def test_the_scan_would_catch_a_regression():
    """Guards the guard: a bare call in an `async def` must be reported."""
    regressed = (
        "async def handler(request):\n"
        "    project = load()\n"
        "    return _find_asset_usages(project, asset)\n"
    )
    assert _on_loop_lock_takers(regressed) == [(3, "_find_asset_usages")]

    hoisted = (
        "async def handler(request):\n"
        "    def work():\n"
        "        return _find_asset_usages(project, asset)\n"
        "    return await asyncio.to_thread(work)\n"
    )
    assert _on_loop_lock_takers(hoisted) == []

    qualified = "async def handler(request):\n    return ps.hydrate_job(p, j)\n"
    assert _on_loop_lock_takers(qualified) == [(2, "hydrate_job")]
