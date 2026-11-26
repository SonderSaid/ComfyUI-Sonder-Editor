"""Low-level atomic file replacement with bounded Windows retry.

``os.replace`` is atomic on POSIX and Windows, but on Windows it raises
``PermissionError`` (WinError 5 ACCESS_DENIED, 32 SHARING_VIOLATION, 33
LOCK_VIOLATION — all surface as ``PermissionError`` in CPython) whenever another
handle holds the destination at that instant: a concurrent reader/writer on
another thread, antivirus, the Search Indexer, a backup job, or an external
viewer. No inter-process lock fixes this; the pragmatic remedy is a short
bounded retry, since the colliding handle is almost always transient (a real
``os.replace`` completes in microseconds).

Our own concurrent writers (the ComfyUI render worker, the bridge daemon thread,
``asyncio.to_thread`` workers, and the event-loop routes) are serialized by the
per-project commit lock in ``project_manager.save_project`` (across processes too when
Share Projects Between ComfyUI Servers is on), so
this ``atomic_replace`` succeeds on the first attempt in the common case. The backoff
is exercised by handle holders outside that lock: antivirus, the Search Indexer, an
external viewer, and another ComfyUI process reading ``project.json``, since loads
take no cross-process lock. Because our writers do not collide, the lock is held for
one save, and never on the event loop. The bounded backoff here is ~375 ms worst case — far
under the 30 s session TTL — and zero on the uncontended common path.
"""

import logging
import os
import random
import time

logger = logging.getLogger("sonder_editor")

# Backend resilience parameters (hard-coded by design — not user settings).
# 5 attempts ⇒ 4 inter-attempt sleeps of 25/50/100/200 ms ≈ 375 ms worst case.
ATOMIC_REPLACE_RETRIES = 5
ATOMIC_REPLACE_BASE_DELAY = 0.025  # seconds
# project.json can be held open by another process for a whole cold read (≈1.6 s total).
ROOT_REPLACE_RETRIES = 7


def atomic_replace(src, dst, *, retries=ATOMIC_REPLACE_RETRIES, base_delay=ATOMIC_REPLACE_BASE_DELAY):
    """``os.replace(src, dst)`` with bounded retry on transient ``PermissionError``.

    On Windows a momentary lock on ``dst`` (or ``src``) makes ``os.replace`` raise
    ``PermissionError``; retrying after a short backoff clears it in almost every
    real case. Only ``PermissionError`` is retried, so genuinely fatal errors
    (disk full, missing source) still fail immediately. Sleeps occur *between*
    attempts only — never after the final failed attempt. On exhaustion the
    already-flushed temp ``src`` is best-effort removed (to avoid leaking a stray
    ``*.tmp``) and the original error is re-raised so a stuck lock fails loud.
    """
    last_exc = None
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt + 1 >= retries:
                break
            delay = base_delay * (2 ** attempt)
            time.sleep(delay + random.uniform(0, delay * 0.25))

    # Exhausted: clean up the orphan temp, then surface the real error.
    try:
        if os.path.isfile(src):
            os.remove(src)
    except OSError:
        pass
    logger.warning(
        "atomic_replace exhausted %d retries replacing %s -> %s: %s",
        retries, src, dst, last_exc,
    )
    raise last_exc


def atomic_replace_root(src, dst):
    """``atomic_replace`` for ``project.json``, with the longer budget."""
    atomic_replace(src, dst, retries=ROOT_REPLACE_RETRIES)
