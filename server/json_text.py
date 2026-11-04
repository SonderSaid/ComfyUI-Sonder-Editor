"""JSON text that can always be written as UTF-8.

ComfyUI accepts a prompt whose strings hold a lone surrogate (``"\\ud800"`` in
the API JSON): Python decodes it into a ``str`` that cannot be encoded as UTF-8.
Every writer that serializes run data with ``ensure_ascii=False`` and then
encodes UTF-8 raises on it, which used to fail the whole save (Hardening
Phase 10).

``dumps_utf8`` keeps today's bytes for every encodable value and escapes only
the lone surrogates themselves (``\\udXXX``), so readers parse the same value and
every other byte is unchanged. It is for text that leaves the project (a file's
embedded metadata) and for hashes. Durable project data never holds a lone
surrogate: a save node ``scrub``s what it records (U+FFFD), and the project
writers stay strict, so a project can never become unreadable through one.
"""

from __future__ import annotations

import json
import re
from typing import Any

# In a decoded Python str every surrogate code point is unpaired: the JSON
# decoder joins a valid pair into one astral code point.
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
REPLACEMENT = "�"


def _escape(match: re.Match) -> str:
    return "\\u%04x" % ord(match.group())


def dumps_utf8(value: Any, **kwargs: Any) -> str:
    """``json.dumps(value, ensure_ascii=False, **kwargs)`` with each lone
    surrogate written as its ``\\udXXX`` escape, so the text encodes as UTF-8."""
    kwargs.pop("ensure_ascii", None)
    text = json.dumps(value, ensure_ascii=False, **kwargs)
    # ``json.dumps`` writes non-ASCII characters raw only inside string
    # literals, so escaping just the surrogate code points keeps the JSON valid.
    return _LONE_SURROGATE.sub(_escape, text) if _LONE_SURROGATE.search(text) else text


def has_lone_surrogate(text: str) -> bool:
    return bool(_LONE_SURROGATE.search(text))


def scrub(value: Any) -> Any:
    """``value`` with every lone surrogate in its strings (keys included)
    replaced by U+FFFD. Containers are copied only when something changes.
    U+FFFD is one UTF-16 code unit, like the surrogate it replaces, so the
    collector's UTF-16 ``raw_field_spans`` stay valid."""
    if isinstance(value, str):
        return _LONE_SURROGATE.sub(REPLACEMENT, value) if _LONE_SURROGATE.search(value) else value
    if isinstance(value, list):
        items = [scrub(item) for item in value]
        return items if any(a is not b for a, b in zip(items, value)) else value
    if isinstance(value, dict):
        pairs = [(scrub(key), scrub(item)) for key, item in value.items()]
        changed = any(k is not k0 or v is not v0 for (k, v), (k0, v0) in zip(pairs, value.items()))
        return dict(pairs) if changed else value
    return value
