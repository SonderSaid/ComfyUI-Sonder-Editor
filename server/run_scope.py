"""What a prompt can execute: the static upstream closure of its execution roots.

ComfyUI runs only the output nodes it queued (``outputs_to_execute``, which a
partial run narrows) and whatever they depend on. Sonder's helpers and its output
observer read that closure instead of the whole prompt.

The closure over-approximates: lazy inputs a run never requests are inside it. It
is only a safe bound while no node can add links at run time to nodes outside it,
so a prompt holding a node that sees the dynamic prompt or the execution list has
an unknown scope (None), except the expanders listed below, whose expansions only
clone their own contained body. Callers treat None as the whole prompt.

Pure except ``expander_classes``, which asks ComfyUI's node definitions lazily,
once per class, and only for classes the prompt uses.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from .prompt_graph import upstream_set

logger = logging.getLogger(__name__)

# Expansions that link only inside their own contained body (Guides Bridge loops).
CONTAINED_EXPANDERS = frozenset({"SonderGuidesBridgeEnd"})
_RUNTIME_GRAPH_HIDDEN = frozenset({"DYNPROMPT", "EXECUTION_LIST"})

_LOCK = threading.Lock()
_EXPANDER_BY_CLASS: dict[str, bool] = {}


def _hidden_name(value: Any) -> str:
    """A hidden input's type name. V1 nodes write ``"DYNPROMPT"``; V3's ``INPUT_TYPES``
    writes a one-element tuple, ``("DYNPROMPT",)``; either may be a str Enum."""
    if isinstance(value, (tuple, list)):
        value = value[0] if value else ""
    return str(getattr(value, "value", value))


def _class_sees_runtime_graph(class_type: str) -> bool:
    with _LOCK:
        known = _EXPANDER_BY_CLASS.get(class_type)
    if known is not None:
        return known
    answer = False
    try:
        import nodes as comfy_nodes  # ComfyUI's module; absent in plain unit tests
        class_def = comfy_nodes.NODE_CLASS_MAPPINGS.get(class_type)
        if class_def is not None:
            hidden = (class_def.INPUT_TYPES() or {}).get("hidden") or {}
            answer = any(_hidden_name(value) in _RUNTIME_GRAPH_HIDDEN for value in hidden.values())
    except ImportError:
        answer = False
    except Exception:
        # A definition that cannot describe itself is treated as able to expand.
        logger.warning("Could not read node definition %s; its prompt's scope is unknown", class_type, exc_info=True)
        answer = True
    with _LOCK:
        _EXPANDER_BY_CLASS[class_type] = answer
    return answer


def compute(prompt: Any, roots: Any) -> frozenset | None:
    """The execution scope, or None when it cannot be bounded statically."""
    if not isinstance(prompt, dict) or roots is None:
        return None
    for node in prompt.values():
        class_type = str(node.get("class_type") or "") if isinstance(node, dict) else ""
        if class_type and class_type not in CONTAINED_EXPANDERS and _class_sees_runtime_graph(class_type):
            return None
    return frozenset(upstream_set(prompt, roots))


def for_prompt(prompt: Any) -> frozenset | None:
    """The execution scope of the prompt ComfyUI is running now, or None (unknown).

    Read through the prompt's open release record, so it needs no registration:
    any prompt, from any client, gets it. Unknown means the whole prompt.
    """
    from . import sdk_runs
    key = sdk_runs.trusted_key_for_prompt(prompt)
    return sdk_runs.execution_scope(key, compute) if key else None


def reset_for_tests(known: dict[str, bool] | None = None) -> None:
    with _LOCK:
        _EXPANDER_BY_CLASS.clear()
        _EXPANDER_BY_CLASS.update(known or {})
