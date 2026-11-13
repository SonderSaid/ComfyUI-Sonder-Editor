"""Media identity for the cache pipeline's ``DESCRIPTOR`` bridges (M2).

An identified bridge (``SONDER_CACHE_IDENTITY``, ``nodes/cache_policy.py``)
splits its decode in two:

- ``cache_recipe(**inputs)``: a classmethod that reads the handle or project and
  returns a JSON-safe **recipe**: every value the decode uses that is not file
  content (resolved absolute paths, asset and member fields, spans, sizes, fit,
  the resolved colour interpretation), plus ``sources``, the files it opens.
- a decoder that reads only the recipe and the files it names.

Before cache keys exist, ``server/cache_pipeline.py`` builds the recipe, hashes
it with the full content of every source (``identify``), registers the result
on the run record and replaces the bridge's ``token_input`` link with the token.
ComfyUI then keys the bridge by the token, so an unchanged recipe over unchanged
bytes hits and nothing is decoded; any change to either is a new token.

On the scheduled call, ``resolve`` turns the token back into its recipe through
the run record of the prompt that is executing, never from the string itself, so
a token typed into a prompt cannot name a file. ``reading`` checks every source
against the stat snapshot taken when it was hashed, before and after decode: a
file changed in between fails the run rather than caching new bytes under the
old identity. **A miss is acceptable; a false hit never is.**
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from typing import Any, Callable

from ..server import sdk_runs

try:
    from comfy_execution.utils import get_executing_context
except ImportError:
    # Outside ComfyUI nothing executes a prompt, so no token can be resolved.
    def get_executing_context():
        return None

TOKEN_PREFIX = "sonder-identity:v1:"
CHUNK = 1 << 20
CHECK_EVERY = 16  # chunks between interrupt checks while hashing (16 MiB)
# Every prompt hashes every byte of a bridge's sources, even on a hit, while the
# decode may read only a short span. Past this many bytes the hash can cost more
# than the decode it saves, so the bridge keeps its stock path (a miss).
IDENTITY_MAX_BYTES = 512 * 1024 * 1024


class SourceTooLarge(Exception):
    """A bridge's sources exceed ``IDENTITY_MAX_BYTES``: no identity this run."""


class SourceUnstable(Exception):
    """A source changed while it was being hashed: no identity this run."""


class SourceMissing(Exception):
    """A recipe names a source that is not a readable file: no identity this run."""


class RecipeNotCanonical(Exception):
    """The recipe does not survive a JSON round trip with exact types."""


def is_token(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(TOKEN_PREFIX)


def _same(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(_same(a[key], b[key]) for key in a)
    if isinstance(a, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def canonical(recipe: dict) -> str:
    """The recipe's one serialisation; raises ``RecipeNotCanonical`` unless it round-trips exactly.

    Lists, never tuples; finite floats; string keys. A recipe that changed type
    on the way through JSON could decode differently from the one hashed.
    """
    try:
        text = json.dumps(recipe, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=True)
    except (TypeError, ValueError) as exc:
        raise RecipeNotCanonical(str(exc)) from None
    if not isinstance(recipe, dict) or not _same(json.loads(text), recipe):
        raise RecipeNotCanonical("the recipe changes on a JSON round trip")
    return text


def stat_snapshot(path: str) -> list[int]:
    """Size, modification time, file id and device: what a change between hash and decode moves."""
    st = os.stat(path)
    return [int(st.st_size), int(st.st_mtime_ns), int(st.st_ino), int(st.st_dev)]


def digest_file(path: str, check: Callable[[], None] | None = None) -> tuple[str, list[int]]:
    """``(sha256 hex, stat snapshot)`` of every byte of ``path``, streamed in 1 MiB chunks.

    ``check`` (the executor's interrupt check) runs every 16 MiB, so a cancel
    stops a long hash. A file whose stat moved while it was read is unstable.
    """
    if not path or not os.path.isfile(path):
        raise SourceMissing(path)
    before = stat_snapshot(path)
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        for index, chunk in enumerate(iter(lambda: handle.read(CHUNK), b"")):
            hasher.update(chunk)
            if check is not None and index % CHECK_EVERY == CHECK_EVERY - 1:
                check()
    if stat_snapshot(path) != before:
        raise SourceUnstable(path)
    return hasher.hexdigest(), before


def identify(class_type: str, recipe: dict, check: Callable[[], None] | None = None,
             memo: dict | None = None) -> tuple[str, str, dict]:
    """``(token, recipe JSON, {source: stat snapshot})`` for one recipe.

    The token hashes the class, the canonical recipe and the full content of each
    source in the recipe's own order. ``memo`` (path -> digest and snapshot) lets a
    prompt hash a file shared by several bridges once.
    """
    recipe_json = canonical(recipe)
    sources = recipe.get("sources")
    if not isinstance(sources, list) or not all(isinstance(path, str) for path in sources):
        raise RecipeNotCanonical("sources must be a list of paths")
    total = 0
    for path in sources:
        if not path or not os.path.isfile(path):
            raise SourceMissing(path)
        total += os.path.getsize(path)
    if total > IDENTITY_MAX_BYTES:
        raise SourceTooLarge(total)
    hasher = hashlib.sha256()
    hasher.update(str(class_type).encode("utf-8") + b"\0")
    hasher.update(recipe_json.encode("utf-8") + b"\0")
    snapshots: dict[str, list[int]] = {}
    for path in sources:
        if memo is not None and path in memo:
            digest, snapshot = memo[path]
        else:
            digest, snapshot = digest_file(path, check)
            if memo is not None:
                memo[path] = (digest, snapshot)
        snapshots[path] = snapshot
        hasher.update(path.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\0")
    return TOKEN_PREFIX + hasher.hexdigest(), recipe_json, snapshots


def resolve(value: Any, class_type: str, build: Callable[[Any], dict], connect_hint: str) -> tuple[dict, dict | None]:
    """``(recipe, snapshots)`` for a bridge's ``token_input`` value.

    A token resolves only through the run record of the prompt executing now; its
    snapshots come back for ``reading``. Anything else is the stock path: the
    recipe is built from the value as it is, and nothing is checked.
    """
    if not is_token(value):
        return build(value), None
    context = get_executing_context()
    entry = sdk_runs.identity_for(getattr(context, "prompt_id", None), value) if context is not None else None
    if entry is None or entry[0] != class_type:
        raise RuntimeError(f"This input holds a media identity this run did not create: {connect_hint}.")
    _class_type, recipe_json, snapshots = entry
    return json.loads(recipe_json), snapshots


def _check_sources(snapshots: dict) -> None:
    for path, snapshot in snapshots.items():
        try:
            current = stat_snapshot(path)
        except OSError:
            current = None
        if current != snapshot:
            raise RuntimeError(f"{os.path.basename(path)} changed while this run was reading it. Run again.")


@contextlib.contextmanager
def reading(snapshots: dict | None):
    """Decode a token's recipe: every source must match its hashed stat before and after.

    The stock path (``snapshots`` None) checks nothing, as before Phase 3.
    """
    if snapshots:
        _check_sources(snapshots)
    yield
    if snapshots:
        _check_sources(snapshots)
