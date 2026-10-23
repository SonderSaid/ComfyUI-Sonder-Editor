"""Cancel an owned prompt, and whether this ComfyUI can run one output branch.

**Cancel** uses ComfyUI's own atomic queue operations, never ``/interrupt``
(which interrupts whatever runs now): a queued prompt is deleted, a running one
is interrupted only if it is still the one running. Two windows remain where
ComfyUI itself would miss it, and the cancel flag ``sdk_runs`` keeps closes both:

- between a client-chosen prompt id and its ``put`` (``validate_prompt`` runs
  first): the ``put`` hook deletes a flagged prompt right after queueing it;
- between ``get`` and ComfyUI resetting its interrupt flag at the start of
  ``execute_async``: the ``send_sync`` observer interrupts a flagged prompt again
  on its ``execution_start``, which ComfyUI sends after that reset (to prompts
  with a client id, as every branch run has).

**Branch runs** (``partial_execution_targets``) are offered only where the
verified behaviour holds: ComfyUI 0.42.0 or later, the atomic queue operations
present, and a ``validate_prompt`` that takes partial targets. A preassigned
prompt id that comes back different turns the capability off for the process.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from . import sdk_runs

logger = logging.getLogger(__name__)

MIN_COMFYUI_VERSION = (0, 42, 0)
_DISABLED_REASON = ""

CANCEL_DEQUEUED, CANCEL_INTERRUPTED, CANCEL_FLAGGED = "dequeued", "interrupted", "flagged"
CANCEL_UNSUPPORTED = "unsupported"


def _prompt_queue():
    from server import PromptServer  # ComfyUI's module
    return PromptServer.instance.prompt_queue


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in str(text or "").split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def branch_capability() -> dict[str, Any]:
    """``{"ok": bool, "reason": str}``: whether a branch run may be offered here."""
    if _DISABLED_REASON:
        return {"ok": False, "reason": _DISABLED_REASON}
    try:
        import comfyui_version
        version = _version_tuple(getattr(comfyui_version, "__version__", ""))
    except Exception:
        return {"ok": False, "reason": "unknown_comfyui_version"}
    if version < MIN_COMFYUI_VERSION:
        return {"ok": False, "reason": "comfyui_too_old"}
    try:
        queue = _prompt_queue()
        import execution
        params = list(inspect.signature(execution.validate_prompt).parameters)
    except Exception:
        return {"ok": False, "reason": "comfyui_unavailable"}
    if not callable(getattr(queue, "interrupt_if_running", None)) or not callable(getattr(queue, "delete_queue_item", None)):
        return {"ok": False, "reason": "no_atomic_cancel"}
    if len(params) < 3:
        return {"ok": False, "reason": "no_partial_execution"}
    # The Sonder App posts its branches to this ComfyUI's own address (HTTP only).
    try:
        from .sdk_proxy import own_upstream_url
        own_upstream_url()
    except Exception as exc:
        return {"ok": False, "reason": str(getattr(exc, "code", "") or "no_local_address")}
    return {"ok": True, "reason": ""}


def disable_branch_runs(reason: str) -> None:
    """Turn branch runs off for this process (ComfyUI behaved unlike the verified version)."""
    global _DISABLED_REASON
    if not _DISABLED_REASON:
        logger.warning("Branch runs turned off: %s", reason)
    _DISABLED_REASON = str(reason or "disabled")


def cancel_prompt(prompt_id: Any, prompt_queue: Any = None) -> str:
    """Cancel one prompt by id. Returns how: dequeued, interrupted, or flagged only.

    "flagged" also covers a prompt ComfyUI has not queued yet or has just taken:
    the hooks act on the flag when it gets there. Finished prompts are untouched.
    """
    prompt_id = str(prompt_id or "")
    queue = prompt_queue if prompt_queue is not None else _prompt_queue()
    interrupt = getattr(queue, "interrupt_if_running", None)
    if not callable(interrupt) or not callable(getattr(queue, "delete_queue_item", None)):
        # Older ComfyUI: no atomic way to cancel only this prompt.
        return CANCEL_UNSUPPORTED
    sdk_runs.request_cancel(prompt_id)
    if queue.delete_queue_item(lambda item: len(item) > 1 and item[1] == prompt_id):
        return CANCEL_DEQUEUED
    if interrupt(prompt_id):
        return CANCEL_INTERRUPTED
    return CANCEL_FLAGGED


def after_put(item: Any, prompt_queue: Any) -> None:
    """The ``put`` hook's follow-up: a prompt cancelled before it was queued leaves at once."""
    prompt_id = str(item[1]) if isinstance(item, (tuple, list)) and len(item) > 1 else ""
    if prompt_id and sdk_runs.cancel_requested(prompt_id):
        prompt_queue.delete_queue_item(lambda entry: len(entry) > 1 and entry[1] == prompt_id)


def observe(event: str, data: Any, prompt_queue: Any = None) -> None:
    """``send_sync`` observer: interrupt a cancelled prompt again once ComfyUI reset its flag."""
    if event != "execution_start" or not isinstance(data, dict):
        return
    prompt_id = str(data.get("prompt_id") or "")
    if prompt_id and sdk_runs.cancel_requested(prompt_id):
        queue = prompt_queue if prompt_queue is not None else _prompt_queue()
        queue.interrupt_if_running(prompt_id)


def reset_for_tests() -> None:
    global _DISABLED_REASON
    _DISABLED_REASON = ""
