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
- ``CONTENT``: identity is a digest of the materialized value
  (``nodes/cache_content.py``). The pipeline hands consumers an ephemeral carrier
  keyed by that digest (``nodes/media_carrier.py``). Only on ``IMAGE``/``AUDIO``
  outputs, with the ``CONTENT_INVALIDATION`` cases registered.
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

**Preparation** (``SONDER_CACHE_PREPARE = PREPARES(publishes=(...))``): a node
other than the Editor with an ``EXACT`` output (or a ``HANDLE`` another prepared
node needs) opts in to being run by the cache pipeline before cache keys exist.
The pipeline calls the node's own ``execute`` exactly as the executor would, so
both paths publish through the same lines. The contract, which
``tests/test_cache_pipeline_bridges.py`` enforces: ``execute`` reads the project
and the frozen job, publishes JSON-safe values under the declared
``_execution_context`` keys only, reads nothing a save mutates, and does nothing
else (no file writes, claims, receipts, run-record changes).

**Routers** (``SONDER_CACHE_LAZY = LAZY_REQUESTS(method, deciding)``): a Sonder
lazy router whose ``method`` is the one rule for which lazy inputs it requests,
called by its own ``check_lazy_status`` and by the pipeline with the
``deciding`` inputs' values when they are known before cache keys. The pipeline
then treats the requested branch as certain to run.

**Identity** (``SONDER_CACHE_IDENTITY = IDENTIFIES(token_input, recipe_inputs)``):
a bridge whose ``DESCRIPTOR`` outputs come from a decode recipe
(``cache_recipe``) plus the bytes of the files it names. The pipeline hashes
both before cache keys and hands the bridge a token in place of its
``token_input`` link (M2); ``nodes/cache_identity.py`` owns the contract.

**Deferred media** (``SONDER_CACHE_DEFERRED = DEFERS(outputs=(...))``, Phase 4b):
the Editor's ``DESCRIPTOR`` media outputs whose identity is computed before
they are materialized (``nodes/editor_media.py``): a recipe of everything the
render or mix reads, plus the full content of every file it names. The early
run leaves those slots deferred; the pipeline hands their consumers a carrier
keyed by that identity and settles each slot in ComfyUI's ``set_prompt``, from
the cache on a hit, by rendering on a miss.

The ``SONDER_`` prefix keeps these names clear of any future ComfyUI attribute.
"""

from __future__ import annotations

import fnmatch
import logging
import math
import numbers
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

EXACT = "EXACT"
DESCRIPTOR = "DESCRIPTOR"
CONTENT = "CONTENT"
PER_RUN = "PER_RUN"
HANDLE = "HANDLE"

# Socket types a CONTENT output may have (cache_content.digest supports exactly these).
CONTENT_TYPES = ("IMAGE", "AUDIO")
# The invalidation cases every CONTENT output registers
# (tests/test_cache_pipeline_executor.py runs them on the real executor): a change
# to the output's content reruns its consumers; equal content, whatever produced
# it, reuses them; the timeline's layout (clips moved without changing what
# renders) is not part of the identity.
CONTENT_INVALIDATION = ("content_changes", "equal_content", "layout_independent")

# Socket type -> the exact Python type an EXACT value must have to become a literal.
LITERAL_TYPES = {"STRING": str, "INT": int, "FLOAT": float, "BOOLEAN": bool}

TAKES_PROJECT = "takes SONDER_PROJECT"
NAN_FINGERPRINT = "NaN fingerprint"
PROOFS = (TAKES_PROJECT, NAN_FINGERPRINT)

POLICY_ATTRIBUTE = "SONDER_CACHE_POLICY"
EXECUTION_ATTRIBUTE = "SONDER_CACHE_EXECUTION"
PREPARE_ATTRIBUTE = "SONDER_CACHE_PREPARE"
LAZY_ATTRIBUTE = "SONDER_CACHE_LAZY"
IDENTITY_ATTRIBUTE = "SONDER_CACHE_IDENTITY"
DEFER_ATTRIBUTE = "SONDER_CACHE_DEFERRED"
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


@dataclass(frozen=True)
class Prepares:
    publishes: Any


def PREPARES(publishes=()) -> Prepares:  # noqa: N802
    """The node may be run early by the cache pipeline; ``publishes`` names the
    ``_execution_context`` keys its ``execute`` writes (a tuple of strings)."""
    return Prepares(tuple(publishes) if isinstance(publishes, (tuple, list)) else publishes)


@dataclass(frozen=True)
class LazyRequests:
    method: str
    deciding: tuple


def LAZY_REQUESTS(method: str, deciding=()) -> LazyRequests:  # noqa: N802
    """``method(deciding: dict, connected: frozenset) -> list[str]`` is the router's one lazy rule."""
    return LazyRequests(str(method or ""), tuple(deciding))


@dataclass(frozen=True)
class Identifies:
    token_input: str
    recipe_inputs: tuple
    passthrough: tuple


def IDENTIFIES(token_input: str, recipe_inputs=(), passthrough=()) -> Identifies:  # noqa: N802
    """The class's ``DESCRIPTOR`` outputs are identified by ``cache_recipe`` plus file content.

    ``token_input`` is the project-derived input the pipeline replaces with the
    identity token; ``recipe_inputs`` are the other inputs ``cache_recipe`` reads
    (each must be a literal); ``passthrough`` names output patterns that only
    forward linked inputs (declared ``INHERITS``).
    """
    return Identifies(str(token_input or ""), tuple(recipe_inputs), tuple(passthrough))


# The generated invalidation cases every identified output registers
# (tests/test_cache_identity.py runs each one for every IDENTIFIES class):
# any input the stock path reads that changes the decode changes the token; a
# different file at the same path, even with the same size and a copied mtime,
# changes it; identical bytes rewritten keep it.
IDENTITY_INVALIDATION = ("inputs_reach_identity", "same_path_content", "unchanged_content")


@dataclass(frozen=True)
class Defers:
    outputs: tuple


def DEFERS(outputs=()) -> Defers:  # noqa: N802
    """The class's named ``DESCRIPTOR`` media outputs are identified before they are materialized."""
    return Defers(tuple(outputs) if isinstance(outputs, (tuple, list)) else outputs)


# The invalidation cases every deferred output registers (tests/test_editor_media.py
# runs each one for every DEFERS class): every input the render or mix reads that
# changes its output changes the identity, fractional and mistyped values included,
# or there is no identity; a different file at the same path changes it; identical
# bytes rewritten keep it; the materializer reads only its recipe and the files it names.
DEFERRED_INVALIDATION = ("inputs_reach_identity", "same_path_content", "unchanged_content", "recipe_only")


def defer_policy(class_def: Any) -> Defers | None:
    declared = getattr(class_def, DEFER_ATTRIBUTE, None)
    return declared if isinstance(declared, Defers) else None


def deferred_slots(class_def: Any) -> tuple[int, ...]:
    """The ``DESCRIPTOR`` media slots the class defers (``DEFERS``), in output order."""
    declared = defer_policy(class_def)
    if declared is None:
        return ()
    types_ = tuple(getattr(class_def, "RETURN_TYPES", None) or ())
    names = tuple(getattr(class_def, "RETURN_NAMES", None) or ())
    return tuple(slot for slot, name in enumerate(names)
                 if name in declared.outputs and slot < len(types_) and types_[slot] in CONTENT_TYPES
                 and output_policy(class_def, name) == DESCRIPTOR)


def carried_slots(class_def: Any) -> tuple[int, ...]:
    """Every media slot a carrier may serve: ``CONTENT`` and deferred ``DESCRIPTOR`` outputs."""
    return tuple(sorted(set(content_slots(class_def)) | set(deferred_slots(class_def))))


def lazy_policy(class_def: Any) -> LazyRequests | None:
    declared = getattr(class_def, LAZY_ATTRIBUTE, None)
    return declared if isinstance(declared, LazyRequests) else None


def identity_policy(class_def: Any) -> Identifies | None:
    declared = getattr(class_def, IDENTITY_ATTRIBUTE, None)
    return declared if isinstance(declared, Identifies) else None


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


def content_slots(class_def: Any) -> tuple[int, ...]:
    """The ``CONTENT`` output slots whose socket type is in ``CONTENT_TYPES``."""
    types_ = tuple(getattr(class_def, "RETURN_TYPES", None) or ())
    names = tuple(getattr(class_def, "RETURN_NAMES", None) or ())
    return tuple(slot for slot, name in enumerate(names)
                 if slot < len(types_) and types_[slot] in CONTENT_TYPES and output_policy(class_def, name) == CONTENT)


def execution_policy(class_def: Any) -> Any:
    """The node-level declaration, or None."""
    return getattr(class_def, EXECUTION_ATTRIBUTE, None)


def prepare_policy(class_def: Any) -> Prepares | None:
    """The class's ``PREPARES`` declaration, or None."""
    declared = getattr(class_def, PREPARE_ATTRIBUTE, None)
    return declared if isinstance(declared, Prepares) else None


def exact_slots(class_def: Any, kind: str | None = None) -> tuple[int, ...]:
    """The ``EXACT`` output slots (of socket type ``kind``, if given), from the class's declarations."""
    types_ = tuple(getattr(class_def, "RETURN_TYPES", None) or ())
    names = tuple(getattr(class_def, "RETURN_NAMES", None) or ())
    return tuple(slot for slot, name in enumerate(names)
                 if slot < len(types_) and (kind is None or types_[slot] == kind)
                 and output_policy(class_def, name) == EXACT)


def normalise_outputs(class_def: Any, result: Any) -> Any:
    """``result`` with exact-equivalent canonical types on the ``EXACT`` slots; never raises.

    ComfyUI keys ``24`` and ``24.0`` alike, and ``0.0`` and ``-0.0`` alike, so the
    cache pipeline only hands a consumer a literal whose type is exactly its
    socket's, and every producer of an ``EXACT`` output returns through here.
    Only lossless conversions happen: an integral float or a numpy integer on an
    INT slot, an int or a numpy float on a FLOAT slot (``-0.0`` becomes ``0.0``),
    a ``str`` subclass on a STRING slot. Anything else (None, NaN, ``1280.5``, a
    bool, an unexpected shape) is left as it is.
    """
    try:
        if not isinstance(result, tuple) or len(result) != len(getattr(class_def, "RETURN_TYPES", None) or ()):
            return result
        values = list(result)
        for slot in exact_slots(class_def, "INT"):
            value = values[slot]
            if type(value) is int or isinstance(value, bool):
                continue
            if isinstance(value, numbers.Integral):
                values[slot] = int(value)
            elif isinstance(value, numbers.Real) and math.isfinite(value) and float(value).is_integer():
                values[slot] = int(value)
        for slot in exact_slots(class_def, "FLOAT"):
            value = values[slot]
            if isinstance(value, bool) or not isinstance(value, numbers.Real):
                continue
            converted = float(value)
            if not math.isfinite(converted) or converted != value:
                continue  # NaN, infinite, or beyond float precision: not exact
            values[slot] = 0.0 if converted == 0.0 else converted
        for slot in exact_slots(class_def, "STRING"):
            value = values[slot]
            if isinstance(value, str) and type(value) is not str:
                values[slot] = str.__str__(value)
        return tuple(values)
    except Exception:
        logger.debug("Output normalisation skipped for %s", getattr(class_def, "__name__", class_def), exc_info=True)
        return result
