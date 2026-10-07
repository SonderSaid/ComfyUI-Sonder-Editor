"""This node pack's version, read once from ``pyproject.toml``.

A regex rather than ``tomllib``: ComfyUI may run on Python 3.10, which has none.
"" when the file is missing or has no version, so callers never fail on it.
"""

from __future__ import annotations

import functools
import os
import re

_PYPROJECT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pyproject.toml")
_VERSION_RE = re.compile(r'^\s*version\s*=\s*"([^"]+)"\s*$', re.MULTILINE)


@functools.lru_cache(maxsize=1)
def pack_version() -> str:
    try:
        with open(_PYPROJECT, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return ""
    # Only the [project] table's version: stop at the next table header.
    start = text.find("[project]")
    if start < 0:
        return ""
    section = text[start + len("[project]"):]
    end = re.search(r"^\s*\[", section, re.MULTILINE)
    match = _VERSION_RE.search(section[: end.start()] if end else section)
    return match.group(1) if match else ""
