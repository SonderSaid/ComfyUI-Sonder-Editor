"""Run a node with one-shot side effects at most once per prompt.

ComfyUI can execute a node that already finished, again, within the same prompt:
under ``--cache-none`` (and after RAM-pressure eviction) anything that adds a link
to it late re-stages it, because its output is not cached. A lazy input pulling a
branch in does that, and so does node expansion whose clones keep a link outside
the expansion (Guides Bridge End, native loops). For the Editor that would claim
the SDK token and the queue job again; for Save Video it would write a second
take. A repeat therefore returns the first result.

The result is kept on the prompt's release record (``sdk_runs``), so only a prompt
the queue hook opened can reuse one, and it goes when ComfyUI finishes the prompt.
"""

import logging
from typing import Any, Callable

from ..server import sdk_runs

try:
    from comfy_execution.utils import get_executing_context
except ImportError:
    # Outside ComfyUI (unit tests) nothing maps a node over a list. Inside it the
    # context exists since ComfyUI 0.3.44 (July 2025), far older than this pack supports.
    def get_executing_context():
        return None

logger = logging.getLogger(__name__)


def _call_key(unique_id: Any) -> str:
    """``unique_id`` plus the list element ComfyUI is calling it for, or "" if unknown.

    A node fed lists without ``INPUT_IS_LIST`` is called once per element with the
    same id and prompt; each call is its own result.
    """
    if unique_id in (None, ""):
        return ""
    context = get_executing_context()
    if context is None:
        return f"{unique_id}#0"
    if str(context.node_id) != str(unique_id):
        return ""
    return f"{unique_id}#{context.list_index or 0}"


def run_once_per_prompt(prompt: Any, unique_id: Any, label: str, run: Callable[[], Any]) -> Any:
    """``run()``'s result, or the result it already gave this prompt for this call.

    ``prompt`` must be the hidden ``PROMPT`` input: ComfyUI passes the same object
    for the whole prompt, which is how the run's key is found. Without a trusted key
    (no queue hook) every call runs. A raising ``run()`` records nothing.
    """
    key = sdk_runs.trusted_key_for_prompt(prompt)
    call_key = _call_key(unique_id) if key else ""
    if call_key:
        found, value = sdk_runs.first_result(key, call_key)
        if found:
            logger.info("%s %s ran again within one prompt; reusing its first result", label, unique_id)
            if isinstance(value, dict) and "ui" in value:
                # ComfyUI keeps the first run's preview; sending it again would remount it.
                value = {k: v for k, v in value.items() if k != "ui"}
            return value
    value = run()
    if call_key:
        sdk_runs.remember_result(key, call_key, value)
    return value
