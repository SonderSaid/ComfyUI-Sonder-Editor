"""Whether ComfyUI is busy, and whether it has been idle for a while.

Two background workers ask: the Launcher's thumbnails (busy or not, right now) and
proxy media (idle continuously for a grace period). "Idle for" cannot be read from a
single sample, so the prompt-queue hooks report every prompt they see start or end
(``note_busy``) and each sample that finds work does the same. A prompt that starts
and ends between two samples is still seen through the hooks.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

_lock = threading.Lock()
# Unknown history counts as busy: the grace period also runs from startup, so a prompt
# queued while ComfyUI was still loading is not raced.
_last_busy_at = time.monotonic()
_listeners: list[Callable[[], None]] = []


def comfy_queue_busy() -> bool:
    """Whether ComfyUI has a prompt running or waiting. False when it can't be asked."""
    try:
        from server import PromptServer

        running, queued = PromptServer.instance.prompt_queue.get_current_queue_volatile()
        return bool(running or queued)
    except Exception:
        return False


def note_busy(now: float | None = None) -> None:
    """A prompt started or ended, or a sample found work. Wakes the listeners."""
    global _last_busy_at
    with _lock:
        _last_busy_at = max(_last_busy_at, time.monotonic() if now is None else now)
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener()
        except Exception:
            pass


def add_busy_listener(listener: Callable[[], None]) -> None:
    with _lock:
        if listener not in _listeners:
            _listeners.append(listener)


def remove_busy_listener(listener: Callable[[], None]) -> None:
    with _lock:
        if listener in _listeners:
            _listeners.remove(listener)


def seconds_since_busy(*, queue_busy: Callable[[], bool] = comfy_queue_busy,
                       now: float | None = None) -> float:
    """0 while ComfyUI has work, else how long it has had none."""
    current = time.monotonic() if now is None else now
    if queue_busy():
        note_busy(current)
        return 0.0
    with _lock:
        return max(0.0, current - _last_busy_at)


def idle_for(seconds: float, *, queue_busy: Callable[[], bool] = comfy_queue_busy,
             now: float | None = None) -> bool:
    """The queue has been empty continuously for ``seconds``."""
    return seconds_since_busy(queue_busy=queue_busy, now=now) >= float(seconds)


def _reset_for_tests(last_busy_at: float = 0.0) -> None:
    global _last_busy_at
    with _lock:
        _last_busy_at = last_busy_at
        _listeners.clear()
