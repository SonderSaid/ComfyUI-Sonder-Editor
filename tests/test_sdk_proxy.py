"""The SDK proxy lifecycle: upstream, reuse, spawn and stop (server/sdk_proxy.py)."""

import asyncio
import types

import pytest

from server import sdk_proxy
from server.sdk_proxy import ProxyUnavailable


def args(listen="127.0.0.1", port=8888, tls=None):
    return types.SimpleNamespace(listen=listen, port=port, tls_keyfile=tls, tls_certfile=None)


@pytest.mark.parametrize("listen, expected", [
    ("127.0.0.1", "http://127.0.0.1:8888"),
    ("0.0.0.0", "http://127.0.0.1:8888"),
    ("::", "http://[::1]:8888"),
    ("0.0.0.0,::", "http://127.0.0.1:8888"),
    ("192.168.1.20", "http://192.168.1.20:8888"),
    ("192.168.1.20,127.0.0.1", "http://127.0.0.1:8888"),
    ("fe80::1", "http://[fe80::1]:8888"),
    ("", "http://127.0.0.1:8888"),
])
def test_the_upstream_comes_from_the_command_line(listen, expected):
    assert sdk_proxy.own_upstream_url(args(listen)) == expected


def test_https_comfyui_is_reported_unsupported():
    with pytest.raises(ProxyUnavailable) as exc:
        sdk_proxy.own_upstream_url(args(tls="key.pem"))
    assert exc.value.code == "tls_unsupported"


def test_upstream_matching_treats_localhost_as_loopback():
    assert sdk_proxy._same_upstream("http://localhost:8888/", "http://127.0.0.1:8888")
    assert not sdk_proxy._same_upstream("http://127.0.0.1:8882", "http://127.0.0.1:8888")


def test_an_external_proxy_must_be_local_unless_allowed(monkeypatch):
    monkeypatch.setenv("SONDER_SDK_PROXY_URL", "http://10.0.0.5:8189/")
    with pytest.raises(ProxyUnavailable):
        sdk_proxy.configured_override()
    monkeypatch.setenv("SONDER_SDK_ALLOW_REMOTE", "1")
    monkeypatch.setenv("SONDER_SDK_PROXY_TOKEN", "tok")
    target = sdk_proxy.configured_override()
    assert target.url == "http://10.0.0.5:8189" and target.token == "tok" and not target.managed
    monkeypatch.delenv("SONDER_SDK_PROXY_URL")
    assert sdk_proxy.configured_override() is None


class FakeProcess:
    def __init__(self):
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.terminated = True
        self.alive = False

    def kill(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0


def test_the_real_probe_checks_health_upstream_and_the_jobs_api():
    from aiohttp import web

    async def go():
        state = {"jobs_status": 200}

        async def health(request):
            return web.json_response({"status": "healthy", "upstream": "http://127.0.0.1:8888"})

        async def jobs(request):
            return web.json_response({"jobs": []}, status=state["jobs_status"])

        app = web.Application()
        app.router.add_get("/api/v2/health", health)
        app.router.add_get("/api/v2/jobs", jobs)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        try:
            ok = await sdk_proxy._probe(url, "")
            state["jobs_status"] = 401  # a token-gated proxy we hold no token for
            gated = await sdk_proxy._probe(url, "")
        finally:
            await runner.cleanup()
        free = await sdk_proxy._probe(url, "")
        return ok, gated, free

    ok, gated, free = asyncio.run(go())
    assert ok == ("proxy", "http://127.0.0.1:8888")
    assert gated[0] == "other"
    assert free[0] in ("free", "other")  # refused (or slow to refuse on Windows)


def test_a_port_with_a_listener_is_not_free():
    import socket

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        assert sdk_proxy._port_free(port) is False
    finally:
        listener.close()
    assert sdk_proxy._port_free(port) is True


@pytest.fixture
def world(monkeypatch):
    """Ports as a dict: missing = free, ("proxy", upstream) or ("other", reason)."""
    ports = {}
    spawned = []
    monkeypatch.delenv("SONDER_SDK_PROXY_URL", raising=False)
    monkeypatch.setattr(sdk_proxy, "_comfy_args", lambda: args())
    monkeypatch.setattr(sdk_proxy, "proxy_package_installed", lambda: True)

    async def probe(url, token):
        port = int(url.rsplit(":", 1)[1])
        return ports.get(port, ("free", ""))

    def spawn(port, upstream):
        process = FakeProcess()
        spawned.append((port, upstream, process))
        ports[port] = ("proxy", upstream)
        with sdk_proxy._state_lock:
            sdk_proxy._owned.update(process=process, url=f"http://127.0.0.1:{port}", log=None)
        return process

    monkeypatch.setattr(sdk_proxy, "_probe", probe)
    monkeypatch.setattr(sdk_proxy, "_port_free", lambda port: port not in ports)
    monkeypatch.setattr(sdk_proxy, "_spawn", spawn)
    yield types.SimpleNamespace(ports=ports, spawned=spawned)
    sdk_proxy.stop_owned_proxy()


def test_a_running_proxy_for_this_comfyui_is_reused(world):
    world.ports[8189] = ("proxy", "http://127.0.0.1:8882")  # another ComfyUI's proxy
    world.ports[8190] = ("proxy", "http://localhost:8888")
    target = asyncio.run(sdk_proxy.ensure_proxy())
    assert target.url == "http://127.0.0.1:8190" and not target.managed
    assert world.spawned == []
    # The next call rechecks that proxy instead of scanning again.
    probed = []
    real_probe = sdk_proxy._probe

    async def counting(url, token):
        probed.append(url)
        return await real_probe(url, token)

    sdk_proxy._probe = counting
    try:
        assert asyncio.run(sdk_proxy.ensure_proxy()).url == "http://127.0.0.1:8190"
    finally:
        sdk_proxy._probe = real_probe
    assert probed == ["http://127.0.0.1:8190"]
    # Reused, not owned: stopping does nothing to it.
    assert sdk_proxy.stop_owned_proxy() is False


def test_otherwise_one_is_started_on_the_first_free_port_and_stopped_when_owned(world):
    world.ports[8189] = ("other", "health 404")  # something else entirely
    target = asyncio.run(sdk_proxy.ensure_proxy())
    assert target.url == "http://127.0.0.1:8190" and target.managed
    port, upstream, process = world.spawned[0]
    assert (port, upstream) == (8190, "http://127.0.0.1:8888")
    # The owned process is reused while alive.
    assert asyncio.run(sdk_proxy.ensure_proxy()).url == "http://127.0.0.1:8190"
    assert len(world.spawned) == 1
    assert sdk_proxy.stop_owned_proxy() is True and process.terminated


def test_a_spawn_that_never_gets_ready_falls_through_to_the_next_port(world, monkeypatch):
    calls = []

    async def wait_ready(process, url, upstream):
        calls.append(url)
        return url.endswith(":8190")

    monkeypatch.setattr(sdk_proxy, "_wait_ready", wait_ready)
    target = asyncio.run(sdk_proxy.ensure_proxy())
    assert calls == ["http://127.0.0.1:8189", "http://127.0.0.1:8190"]
    assert target.url == "http://127.0.0.1:8190"
    assert world.spawned[0][2].terminated  # the failed one was stopped


def test_no_proxy_package_is_a_typed_error(world, monkeypatch):
    monkeypatch.setattr(sdk_proxy, "proxy_package_installed", lambda: False)
    with pytest.raises(ProxyUnavailable) as exc:
        asyncio.run(sdk_proxy.ensure_proxy())
    assert exc.value.code == "proxy_not_installed"


def test_a_configured_proxy_fronting_another_comfyui_is_refused(world, monkeypatch):
    monkeypatch.setenv("SONDER_SDK_PROXY_URL", "http://127.0.0.1:9000")
    world.ports[9000] = ("proxy", "http://127.0.0.1:8882")
    with pytest.raises(ProxyUnavailable) as exc:
        asyncio.run(sdk_proxy.ensure_proxy())
    assert exc.value.code == "proxy_upstream_mismatch"
