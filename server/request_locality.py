"""Whether a request comes from the ComfyUI machine itself.

Used by routes that change what the server trusts or installs: the Sonder App
install and uninstall, linking a project folder from outside the projects folder,
and turning external project links on. Everything else stays reachable from a
browser on another machine (``--listen``).
"""

from __future__ import annotations

import ipaddress

from aiohttp import web

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Headers a relaying proxy or tunnel adds; any of them means the peer is not the user.
_FORWARDING_HEADERS = (
    "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP",
    "Via", "CF-Connecting-IP", "True-Client-IP",
)


def is_loopback_request(request: web.Request) -> bool:
    """A request made on this machine, to this machine, not relayed.

    The peer must be loopback, the ``Host`` header must name a loopback host (a
    DNS-rebound page reaches 127.0.0.1 under a foreign Host), and no forwarding
    header may be present (a reverse proxy on this machine makes every peer
    look local).

    Residual: a forwarder that adds none of these headers and sends a loopback
    Host (a raw TCP tunnel such as ``ssh -R``, or a bare nginx ``proxy_pass``)
    is indistinguishable from a local browser. Such a setup already exposes
    ComfyUI's own unauthenticated routes; this gate only adds what its callers do.
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
    if any(request.headers.get(name) for name in _FORWARDING_HEADERS):
        return False
    host = request.headers.get("Host", "")
    if host.startswith("["):
        host_name = host[1:].split("]", 1)[0]
    else:
        host_name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return host_name.lower() in _LOOPBACK_HOSTS


# The refusal for trust changes (linking, turning links on). The browser shows the same
# sentence up front from `MACHINE_ONLY_MESSAGE` in web/js/sdk_setup_client.js; a test
# keeps the two equal.
MACHINE_ONLY_MESSAGE = "Change this on the ComfyUI machine."


def local_only_response(message: str, code: str = "not_local") -> web.Response:
    """The 403 a machine-only route answers to any other caller."""
    return web.json_response({"ok": False, "code": code, "error": message}, status=403)
