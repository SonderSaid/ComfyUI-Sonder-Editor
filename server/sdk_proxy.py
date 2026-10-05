"""The local comfy-api-proxy that SDK runs go through: find, reuse, start and stop it.

comfy-sdk talks to the Comfy API v2 shape, which ComfyUI serves only through
``comfy-api-proxy``. Sonder runs one against its own ComfyUI on demand (session
open or Generate), never at startup:

- **Reuse** a proxy on 8189–8198 whose health reports our ComfyUI as upstream and
  whose jobs API answers (a token-gated proxy we hold no token for is skipped).
- **Spawn** ``python -m comfy_api_proxy.cli run`` on the first free port otherwise.
- **Stop** only a process this server spawned: at ComfyUI exit (``on_cleanup`` and
  ``atexit``, since standalone ComfyUI skips cleanup on Ctrl-C) and when the SDK
  has been idle, which bounds the proxy's never-pruned job records. A proxy left
  behind by a crash is benign: the next start reuses it by its upstream.

An external proxy can be named instead with ``SONDER_SDK_PROXY_URL`` (loopback
only unless ``SONDER_SDK_ALLOW_REMOTE=1``) and ``SONDER_SDK_PROXY_TOKEN``.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .sdk_paths import sonder_user_dir

logger = logging.getLogger("sonder_editor")

PORT_RANGE = range(8189, 8199)
HEALTH_TIMEOUT_SEC = 2.0
SPAWN_WAIT_SEC = 15.0
STOP_GRACE_SEC = 5.0
_LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


@dataclass
class ProxyTarget:
    url: str
    token: str
    managed: bool


class ProxyUnavailable(RuntimeError):
    def __init__(self, message: str, code: str = "proxy_unavailable"):
        super().__init__(message)
        self.code = code


_state_lock = threading.Lock()
_owned: dict[str, Any] = {"process": None, "url": "", "log": None}
_current: dict[str, Any] = {"target": None, "error": "", "checked_at": 0.0}
_ensure_lock: asyncio.Lock | None = None
_ensure_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_ensure_lock() -> asyncio.Lock:
    global _ensure_lock, _ensure_lock_loop
    loop = asyncio.get_running_loop()
    if _ensure_lock is None or _ensure_lock_loop is not loop:
        _ensure_lock, _ensure_lock_loop = asyncio.Lock(), loop
    return _ensure_lock


# ── Upstream (this ComfyUI) ──────────────────────────────────────────────────

def _comfy_args():
    from comfy.cli_args import args  # ComfyUI's parsed command line

    return args


def _listen_host(listen: Any) -> str:
    entries = [item.strip() for item in str(listen or "127.0.0.1").split(",") if item.strip()]
    if not entries:
        return "127.0.0.1"
    for entry in entries:
        if entry.lower() in _LOOPBACK_NAMES:
            return "[::1]" if entry == "::1" else entry
    first = entries[0]
    if first == "0.0.0.0":
        return "127.0.0.1"
    if first == "::":
        return "[::1]"
    try:
        if ipaddress.ip_address(first).version == 6:
            return f"[{first}]"
    except ValueError:
        pass
    return first


def own_upstream_url(args: Any = None) -> str:
    """This ComfyUI's address as a proxy should reach it.

    The port comes from the parsed command line, not ``PromptServer.port``,
    which is only set once the site has started.
    """
    args = args or _comfy_args()
    if getattr(args, "tls_keyfile", None) or getattr(args, "tls_certfile", None):
        raise ProxyUnavailable("Sonder App does not support ComfyUI over HTTPS yet.", "tls_unsupported")
    return f"http://{_listen_host(getattr(args, 'listen', '127.0.0.1'))}:{int(getattr(args, 'port', 8188))}"


def _same_upstream(a: str, b: str) -> bool:
    def norm(url: str) -> tuple[str, int]:
        parts = urlsplit(str(url or "").rstrip("/"))
        host = (parts.hostname or "").lower()
        if host == "localhost":
            host = "127.0.0.1"
        return host, parts.port or 80

    return norm(a) == norm(b)


# ── Configuration ────────────────────────────────────────────────────────────

def _is_loopback_url(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def configured_override() -> ProxyTarget | None:
    url = os.environ.get("SONDER_SDK_PROXY_URL", "").strip().rstrip("/")
    if not url:
        return None
    if urlsplit(url).scheme not in ("http", "https"):
        raise ProxyUnavailable("SONDER_SDK_PROXY_URL must be an http(s) URL.", "proxy_config")
    if not _is_loopback_url(url) and os.environ.get("SONDER_SDK_ALLOW_REMOTE", "") != "1":
        raise ProxyUnavailable(
            "SONDER_SDK_PROXY_URL must be on this machine unless SONDER_SDK_ALLOW_REMOTE=1.", "proxy_config"
        )
    return ProxyTarget(url=url, token=os.environ.get("SONDER_SDK_PROXY_TOKEN", "").strip(), managed=False)


def proxy_package_installed() -> bool:
    from .sdk_install import install_state

    return bool(install_state()["proxy"]["installed"])


# ── Probing ──────────────────────────────────────────────────────────────────

async def _probe(url: str, token: str) -> tuple[str, str]:
    """``("free", "")`` when nothing listens, ``("proxy", upstream)`` for a usable
    proxy, ``("other", reason)`` for anything else."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=HEALTH_TIMEOUT_SEC)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{url}/api/v2/health") as response:
                if response.status != 200:
                    return "other", f"health {response.status}"
                body = await response.json(content_type=None)
            upstream = str((body or {}).get("upstream") or "") if isinstance(body, dict) else ""
            if (body or {}).get("status") != "healthy" or not upstream:
                return "other", "not a comfy-api-proxy"
            async with session.get(f"{url}/api/v2/jobs", params={"limit": "1"}, headers=headers) as response:
                if response.status != 200:
                    return "other", f"jobs {response.status}"
            return "proxy", upstream
    except (aiohttp.ClientConnectorError, ConnectionRefusedError):
        return "free", ""
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        return "other", type(exc).__name__


def _port_free(port: int) -> bool:
    """Whether nothing listens on 127.0.0.1:port, by binding it (instant).

    Probing a closed port over HTTP is slow on Windows: a refused connection is
    retried for about two seconds, so a scan of ten ports by HTTP alone could
    time out on every one of them.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


# ── Process management ───────────────────────────────────────────────────────

LOG_MAX_BYTES = 2 * 1024 * 1024


def _log_path() -> str:
    base = sonder_user_dir()
    if not base:
        return ""
    folder = os.path.join(base, "logs")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "sdk_proxy.log")


def _open_log(path: str):
    """Append, so a crashed proxy's output survives its restart; start over past a cap."""
    mode = "w" if os.path.isfile(path) and os.path.getsize(path) > LOG_MAX_BYTES else "a"
    log = open(path, mode, encoding="utf-8")
    log.write(f"\n--- Sonder started the SDK proxy at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
    log.flush()
    return log


def _spawn(port: int, upstream: str) -> subprocess.Popen:
    command = [
        sys.executable, "-m", "comfy_api_proxy.cli", "run",
        "--comfyui", upstream, "--host", "127.0.0.1", "--port", str(port),
    ]
    with _state_lock:
        stale_log = _owned["log"]
        _owned["log"] = None
    if stale_log is not None:  # the previous owned proxy exited on its own
        try:
            stale_log.close()
        except OSError:
            pass
    log_path = _log_path()
    log = _open_log(log_path) if log_path else subprocess.DEVNULL
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags,
        )
    except Exception:
        if log is not subprocess.DEVNULL:
            log.close()
        raise
    with _state_lock:
        _owned["process"] = process
        _owned["url"] = f"http://127.0.0.1:{port}"
        _owned["log"] = log if log is not subprocess.DEVNULL else None
    return process


def _owned_alive() -> tuple[subprocess.Popen | None, str]:
    with _state_lock:
        process, url = _owned["process"], _owned["url"]
    if process is not None and process.poll() is None:
        return process, url
    return None, ""


def stop_owned_proxy() -> bool:
    """Stop the proxy this server spawned, if any. Safe from any thread and at exit."""
    with _state_lock:
        process, log = _owned["process"], _owned["log"]
        _owned.update(process=None, url="", log=None)
        _current.update(target=None)
    stopped = False
    if process is not None and process.poll() is None:
        try:
            process.terminate()
            try:
                process.wait(timeout=STOP_GRACE_SEC)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=STOP_GRACE_SEC)
            stopped = True
            logger.info("Stopped the SDK proxy Sonder started.")
        except Exception:
            logger.exception("Failed to stop the SDK proxy")
    if log is not None:
        try:
            log.close()
        except OSError:
            pass
    return stopped


async def on_cleanup(_app=None) -> None:
    await asyncio.to_thread(stop_owned_proxy)


async def _wait_ready(process: subprocess.Popen, url: str, upstream: str) -> bool:
    deadline = time.monotonic() + SPAWN_WAIT_SEC
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        kind, found = await _probe(url, "")
        if kind == "proxy" and _same_upstream(found, upstream):
            return True
        await asyncio.sleep(0.25)
    return False


async def ensure_proxy() -> ProxyTarget:
    """A usable proxy in front of this ComfyUI, starting one if needed."""
    async with _get_ensure_lock():
        try:
            target = await _ensure_locked()
        except ProxyUnavailable as exc:
            _current.update(target=None, error=str(exc), checked_at=time.time())
            raise
        _current.update(target=target, error="", checked_at=time.time())
        return target


async def _ensure_locked() -> ProxyTarget:
    upstream = own_upstream_url()
    override = configured_override()
    if override is not None:
        kind, found = await _probe(override.url, override.token)
        if kind != "proxy":
            raise ProxyUnavailable(f"The configured SDK proxy at {override.url} is not answering.")
        if not _same_upstream(found, upstream):
            raise ProxyUnavailable(
                f"The configured SDK proxy fronts {found}, not this ComfyUI ({upstream}).", "proxy_upstream_mismatch"
            )
        return override

    if not await asyncio.to_thread(proxy_package_installed):
        raise ProxyUnavailable("The comfy-api-proxy package is not installed.", "proxy_not_installed")

    process, url = _owned_alive()
    if process is not None:
        kind, found = await _probe(url, "")
        if kind == "proxy" and _same_upstream(found, upstream):
            return ProxyTarget(url=url, token="", managed=True)
        await asyncio.to_thread(stop_owned_proxy)

    previous = _current.get("target")
    if previous is not None and not previous.managed:
        # The proxy reused last time: one check instead of a port scan.
        kind, found = await _probe(previous.url, previous.token)
        if kind == "proxy" and _same_upstream(found, upstream):
            return previous

    free_ports = []
    for port in PORT_RANGE:
        if _port_free(port):
            free_ports.append(port)
            continue
        url = f"http://127.0.0.1:{port}"
        kind, found = await _probe(url, "")
        if kind == "proxy" and _same_upstream(found, upstream):
            logger.info("Reusing the SDK proxy at %s.", url)
            return ProxyTarget(url=url, token="", managed=False)
        if kind == "free":
            free_ports.append(port)
    for port in free_ports:
        url = f"http://127.0.0.1:{port}"
        try:
            process = await asyncio.to_thread(_spawn, port, upstream)
        except OSError as exc:
            raise ProxyUnavailable(f"Could not start the SDK proxy: {exc}") from exc
        if await _wait_ready(process, url, upstream):
            logger.info("Started the SDK proxy at %s for %s.", url, upstream)
            return ProxyTarget(url=url, token="", managed=True)
        await asyncio.to_thread(stop_owned_proxy)
    raise ProxyUnavailable("Could not start the SDK proxy on ports 8189–8198.")


def proxy_status() -> dict:
    process, url = _owned_alive()
    target = _current.get("target")
    try:
        upstream = own_upstream_url()
        upstream_error = ""
    except ProxyUnavailable as exc:
        upstream, upstream_error = "", str(exc)
    return {
        "installed": proxy_package_installed(),
        "running": bool(target) or process is not None,
        "url": (target.url if target else url) or "",
        "managed": bool(process is not None),
        "upstream": upstream,
        "error": upstream_error or _current.get("error", ""),
    }
