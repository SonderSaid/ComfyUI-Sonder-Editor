"""Cache policy vocabulary: how each Sonder output and per-run node meets ComfyUI's cache.

The Editor's ``IS_CHANGED`` is NaN, so everything downstream of it misses
ComfyUI's cache by default. The cache pipeline (``server/cache_pipeline.py``)
restores caching where it can prove a value is the whole identity. It reads the
declarations here, and ``tests/test_cache_policy_registry.py`` fails when a
selected node declares nothing, so an output cannot be added and missed.

**Output policies** (``SONDER_CACHE_POLICY = {output name or pattern: policy}``
on the class, beside its sockets):

- ``EXACT``: the value is its own identity. The pipeline hands it to consumers as
  a literal (M1), so it cannot false-hit. Only for ``STRING``/``INT``/``FLOAT``/
  ``BOOLEAN`` outputs.
- ``DESCRIPTOR``: identity computed before materialization (a later phase). Needs
  registered invalidation cases.
- ``PER_RUN``: never cacheable; consumers rerun every prompt.
- ``INHERITS(reason)``: deliberately left on today's behaviour (the Editor's NaN
  reaches consumers). The reason names the phase or limit.
- ``HANDLE``: an intermediate handle (``driver_ref``, ``reference_set``) that
  carries identity to the next Sonder node; not a consumer-facing policy.

A key may be a pattern (``fnmatch``: ``r??``) for count-dependent outputs. An
undeclared output is ``INHERITS("undeclared")`` at runtime.

**Node policies** (``SONDER_CACHE_EXECUTION``) for output nodes and nodes with
hidden ``PROMPT``/``UNIQUE_ID``/``DYNPROMPT``/``EXTRA_PNGINFO``, which may own
per-run work:

- ``ALWAYS_RUNS(proof)``: the node executes whenever scheduled. The proof is
  ``TAKES_PROJECT`` (a required ``SONDER_PROJECT`` input, whose source is never
  substituted and carries the Editor's NaN) or ``NAN_FINGERPRINT`` (its own
  ``IS_CHANGED`` returns NaN). The registry test checks the proof.
- ``CACHEABLE(reason)``: safe to skip on a cache hit; the reason says why.

The ``SONDER_`` prefix keeps these names clear of any future ComfyUI attribute.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any

EXACT = "EXACT"
DESCRIPTOR = "DESCRIPTOR"
PER_RUN = "PER_RUN"
HANDLE = "HANDLE"

# Socket type -> the exact Python type an EXACT value must have to become a literal.
LITERAL_TYPES = {"STRING": str, "INT": int, "FLOAT": float, "BOOLEAN": bool}

TAKES_PROJECT = "takes SONDER_PROJECT"
NAN_FINGERPRINT = "NaN fingerprint"
PROOFS = (TAKES_PROJECT, NAN_FINGERPRINT)

POLICY_ATTRIBUTE = "SONDER_CACHE_POLICY"
EXECUTION_ATTRIBUTE = "SONDER_CACHE_EXECUTION"
# {output name: (case, ...)}: the invalidation cases a DESCRIPTOR output registers.
INVALIDATION_ATTRIBUTE = "SONDER_CACHE_INVALIDATION"


@dataclass(frozen=True)
class Inherits:
    reason: str


@dataclass(frozen=True)
class AlwaysRuns:
    proof: str


@dataclass(frozen=True)
class Cacheable:
    reason: str


def INHERITS(reason: str) -> Inherits:  # noqa: N802 - reads as a policy constant
    return Inherits(str(reason or ""))


def ALWAYS_RUNS(proof: str) -> AlwaysRuns:  # noqa: N802
    return AlwaysRuns(str(proof or ""))


def CACHEABLE(reason: str) -> Cacheable:  # noqa: N802
    return Cacheable(str(reason or ""))


UNDECLARED = Inherits("undeclared")


def declared_policies(class_def: Any) -> dict[str, Any]:
    """The class's own output declarations (``{}`` when it declares none)."""
    declared = getattr(class_def, POLICY_ATTRIBUTE, None)
    return dict(declared) if isinstance(declared, dict) else {}


def declaration_matches(key: str, output_name: str) -> bool:
    """Whether a declaration key (a name, or an ``fnmatch`` pattern) covers ``output_name``."""
    if key == output_name:
        return True
    return any(char in key for char in "*?[") and fnmatch.fnmatchcase(output_name, key)


def output_policy(class_def: Any, output_name: str) -> Any:
    """The declared policy for ``output_name``; an exact name wins over a pattern."""
    declared = declared_policies(class_def)
    if output_name in declared:
        return declared[output_name]
    for key, policy in declared.items():
        if declaration_matches(key, output_name):
            return policy
    return UNDECLARED


def slot_policy(class_def: Any, slot: int) -> Any:
    """The policy for output ``slot``, by its ``RETURN_NAMES`` name."""
    names = tuple(getattr(class_def, "RETURN_NAMES", None) or ())
    if not isinstance(slot, int) or isinstance(slot, bool) or not 0 <= slot < len(names):
        return UNDECLARED
    return output_policy(class_def, names[slot])


def execution_policy(class_def: Any) -> Any:
    """The node-level declaration, or None."""
    return getattr(class_def, EXECUTION_ATTRIBUTE, None)
