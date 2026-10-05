"""Route-level guards of the Sonder App routes (server/sdk_routes.py)."""

import types

import pytest

from server.sdk_routes import is_loopback_request


def request(remote, host="127.0.0.1:8888", **headers):
    return types.SimpleNamespace(remote=remote, headers={"Host": host, **headers})


@pytest.mark.parametrize("remote, host", [
    ("127.0.0.1", "127.0.0.1:8888"),
    ("127.0.0.1", "localhost:8888"),
    ("::1", "[::1]:8888"),
    ("::ffff:127.0.0.1", "localhost"),
])
def test_a_local_request_to_a_local_host_is_loopback(remote, host):
    assert is_loopback_request(request(remote, host))


@pytest.mark.parametrize("req", [
    request("192.168.1.5"),                                   # another machine
    request("127.0.0.1", "evil.example:8888"),                # DNS rebinding
    request("127.0.0.1", "192.168.1.2:8888"),                 # LAN name for this machine
    request("127.0.0.1", **{"X-Forwarded-For": "8.8.8.8"}),   # relayed by a local proxy
    request("127.0.0.1", Forwarded="for=8.8.8.8"),
    request(None),
    request("not-an-ip"),
])
def test_anything_else_is_not(req):
    assert not is_loopback_request(req)
