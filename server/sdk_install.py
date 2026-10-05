"""The optional SDK packages: whether they are installed (and, from Phase 4c, installing them).

comfy-sdk and comfy-api-proxy are not pack dependencies; a user opts in. State is
derived from the Python environment on every call, never stored.
"""

from __future__ import annotations

import importlib
import importlib.metadata

PINNED = {"comfy-sdk": "0.4.0", "comfy-api-proxy": "0.1.6"}


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return ""


def install_state() -> dict:
    """``{"sdk": {"installed", "version"}, "proxy": {...}, "ready"}`` from the environment."""
    importlib.invalidate_caches()
    sdk_version = _version("comfy-sdk")
    proxy_version = _version("comfy-api-proxy")
    sdk_ok = sdk_version.startswith("0.4.")
    proxy_ok = proxy_version.startswith("0.1.")
    return {
        "sdk": {"installed": bool(sdk_version), "version": sdk_version, "supported": sdk_ok},
        "proxy": {"installed": bool(proxy_version), "version": proxy_version, "supported": proxy_ok},
        "ready": sdk_ok and proxy_ok,
    }
