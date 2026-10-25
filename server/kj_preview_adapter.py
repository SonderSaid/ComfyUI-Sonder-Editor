"""Make KJNodes Model Preview Override's encoder thread run in its sampler's context.

KJ's ``_AsyncPreviewEncoder`` encodes each step's preview on a daemon thread and
sends ``kj_preview_override`` from there. A thread inherits no context
variables, so ComfyUI's ``ExecutionContext`` (the prompt and node being
executed) is absent where the event is sent, and the event names no prompt.
This adapter wraps the encoder's ``submit`` so each queued closure runs inside a
copy of the submitting thread's context; ``sampling_previews`` then reads the
prompt from that context, exactly as ComfyUI's own progress hook does.

Narrow by construction: the module is found through ComfyUI's node mapping, its
shape is checked (never a version pin), the class attribute is replaced once
(a marker makes installation idempotent), and the wrapper falls back to the
original call on any failure, so sampling is never interrupted. KJ's files are
never touched.

Expiry: remove this adapter when ``kj_preview_override`` carries ``prompt_id``,
or when KJ publishes through ComfyUI's preview metadata.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

KJ_CLASS = "ModelPreviewOverrideKJ"
MARKER = "_sonder_kj_ctx"
# Each must appear in the wrapper's source: the encoder is built there, the
# closure goes through ``submit``, and the event is sent through ComfyUI's
# ``send_sync`` (which the Sonder wrap observes).
_REQUIRED_TOKENS = ("_AsyncPreviewEncoder(", "encoder.submit(", '"kj_preview_override"',
                    "PromptServer.instance.send_sync(")

STATE_OK, STATE_DEGRADED, STATE_UNSUPPORTED, STATE_ABSENT = "ok", "degraded", "unsupported", "absent"

_LOCK = threading.Lock()
_STATUS: dict[str, Any] = {"state": STATE_ABSENT}
_VERDICT: dict[str, Any] | None = None


def status() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATUS)


def _set(state: str, **fields: Any) -> dict[str, Any]:
    with _LOCK:
        _STATUS.clear()
        _STATUS.update(state=state, **fields)
        return dict(_STATUS)


def note_unattributed() -> None:
    """A KJ event arrived with no context although the adapter is in place."""
    with _LOCK:
        if _STATUS.get("state") == STATE_OK:
            _STATUS["state"] = STATE_DEGRADED
            _STATUS["reason"] = "event_without_context"
            logger.warning("KJ Model Preview Override sent a preview outside its sampler's context; "
                           "its previews can't be matched to a run")


def _kj_version(module: Any) -> str:
    try:
        root = Path(inspect.getfile(module)).resolve().parents[1]
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        found = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        return found.group(1) if found else ""
    except Exception:
        return ""


def check_shape(module: Any) -> str | None:
    """None when the module has the shape this adapter was written for, else the reason."""
    encoder = getattr(module, "_AsyncPreviewEncoder", None)
    wrapper = getattr(module, "_PreviewOverrideWrapper", None)
    if not inspect.isclass(encoder) or not inspect.isclass(wrapper):
        return "classes_missing"
    submit = encoder.__dict__.get("submit")
    if getattr(submit, MARKER, False):
        submit = getattr(submit, "__wrapped__", submit)
    if not inspect.isfunction(submit):
        return "submit_missing"
    try:
        if list(inspect.signature(submit).parameters) != ["self", "fn"]:
            return "submit_signature"
    except (TypeError, ValueError):
        return "submit_signature"
    if not callable(getattr(wrapper, "__call__", None)):
        return "wrapper_call_missing"
    try:
        source = inspect.getsource(wrapper)
    except (OSError, TypeError):
        return "source_unavailable"
    missing = [token for token in _REQUIRED_TOKENS if token not in source]
    return f"tokens_missing:{','.join(missing)}" if missing else None


def _wrap(original):
    @functools.wraps(original)
    def submit(self, fn):
        try:
            # A fresh copy per submission: one context is never entered twice at once.
            bound = functools.partial(contextvars.copy_context().run, fn)
        except Exception:
            return original(self, fn)
        return original(self, bound)

    setattr(submit, MARKER, True)
    return submit


def _locate() -> Any:
    try:
        from .control_descriptors import comfy_class_lookup  # ComfyUI's NODE_CLASS_MAPPINGS
        node_class = comfy_class_lookup(KJ_CLASS)
    except Exception:
        return None
    if node_class is None:
        return None
    return sys.modules.get(getattr(node_class, "__module__", ""))


def install(module: Any = None) -> dict[str, Any]:
    """Install once, or report why not. Memoized: later calls are a dict read."""
    global _VERDICT
    with _LOCK:
        if _VERDICT is not None and module is None:
            return dict(_STATUS)
    try:
        module = module if module is not None else _locate()
        if module is None:
            # KJ may load after Sonder: not memoized, so the startup call retries.
            return _set(STATE_ABSENT)
        version = _kj_version(module)
        reason = check_shape(module)
        if reason is not None:
            logger.warning("KJ Model Preview Override has an unexpected shape (%s, KJNodes %s); "
                           "its previews can't be matched to a run", reason, version or "unknown")
            result = _set(STATE_UNSUPPORTED, reason=reason, version=version)
        else:
            encoder = module._AsyncPreviewEncoder
            if not getattr(encoder.__dict__["submit"], MARKER, False):
                encoder.submit = _wrap(encoder.__dict__["submit"])
            result = _set(STATE_OK, version=version)
        with _LOCK:
            _VERDICT = dict(result)
        return result
    except Exception:
        logger.warning("KJ preview adapter unavailable", exc_info=True)
        return _set(STATE_UNSUPPORTED, reason="install_failed")


def install_on_startup(prompt_server: Any) -> None:
    """Retry once the server starts, after every custom node has loaded."""
    app = getattr(prompt_server, "app", None)
    hooks = getattr(app, "on_startup", None)
    if hooks is None or getattr(app, "_sonder_kj_preview_startup", False):
        return
    try:
        async def _startup(_app):
            install()

        hooks.append(_startup)
        app._sonder_kj_preview_startup = True
    except Exception:
        logger.debug("KJ preview adapter startup hook unavailable", exc_info=True)


def reset_for_tests() -> None:
    global _VERDICT
    with _LOCK:
        _VERDICT = None
        _STATUS.clear()
        _STATUS["state"] = STATE_ABSENT
