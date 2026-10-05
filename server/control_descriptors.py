"""Sonder Control descriptors and the one coercion authority.

``describe_controls(prompt)`` reads an API-format prompt graph and returns what a
Controls panel shows: one descriptor per Sonder Control (its kind, value, bounds,
options and the inputs it feeds), plus the Sonder-node fields a registry exposes
for nodes that take their project from an editor. It reads the *graph*, never a
project, so the change-certificate rule does not apply to it.

``coerce_control_value`` and ``check_value_for_target`` are the only conversion
and validation rules for a Control value. The node's ``execute``, the descriptor
and (later) the SDK's stored values and timeline lanes all go through them.

The module imports only the standard library and ``prompt_graph`` at module
level. ComfyUI's ``nodes``, ``execution`` and ``comfy_api`` are imported lazily,
inside a running ComfyUI, and tests inject fakes instead.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import random
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from . import prompt_graph

logger = logging.getLogger(__name__)

CONTROL_CLASS_TYPE = prompt_graph.SONDER_CONTROL_CLASS_TYPE
PANEL_NOTE_CLASS_TYPE = "SonderPanelNote"
PANEL_NOTE_MAX_CHARS = 4000
PANEL_OPTIONS_LIMIT = 2000
CONTROL_KINDS = ("STRING", "INT", "FLOAT", "BOOLEAN", "COMBO", "SEED")
ANY_KIND = "*"
# Input types that accept any value, so they add no kind of their own.
_WILDCARD_TYPES = frozenset({"*", "COMFY_MATCHTYPE_V3"})

STATUS_OK = "ok"
STATUS_UNLINKED = "unlinked"
STATUS_MIXED = "mixed_targets"
STATUS_UNSUPPORTED = "unsupported"
STATUS_STALE = "stale_kind"
STATUS_DUPLICATE = "duplicate_id"
STATUS_MISSING_ID = "missing_id"

# Shown to the user; the validator's own message goes in the target's ``detail``.
VALIDATOR_REFUSAL = "This input checks its value before the run, so a Control can't set it."

DEFAULT_CONTROL_LABEL = "Sonder Control"

# Mirrors the frontend 1.53.10 number control (`computeNextNumberValue`), which
# keeps randomized values inside +-2^50.
SEED_MODES = ("fixed", "increment", "decrement", "randomize")
_SEED_LIMIT = 2 ** 50

# Bounds on numeric text, checked before conversion: ``int(Decimal("1e1000000"))``
# takes over 30 s, and describe converts whatever a request carries. No target
# takes an integer anywhere near 10^40 (a 64-bit seed is below 10^20).
_NUMBER_TEXT_LIMIT = 64
_INT_EXPONENT_LIMIT = 40

_TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
_FALSE_WORDS = frozenset({"0", "false", "no", "off"})


class ControlValueError(ValueError):
    """A Control value that cannot be converted, or that a target would refuse."""


@dataclass(frozen=True)
class FieldControl:
    """A Sonder-node widget the Controls panel shows without a Control node."""

    class_type: str
    field: str
    label: str


# Entries, not conditionals: add a row to expose another Sonder-node field. Only
# nodes whose ``project`` input reaches an editor are described.
FIELD_CONTROLS: tuple[FieldControl, ...] = (
    FieldControl("SonderSaveVideo", "mark_queue_complete", "Advance queue after take"),
    FieldControl("SonderSaveBridge", "mark_queue_complete", "Advance queue after take"),
    FieldControl("SonderSaveVideo", "place_audio_on_timeline", "Place take audio on timeline"),
    FieldControl("SonderMasksBridge", "edit_video", "Edit video"),
    FieldControl("SonderMasksBridge", "edit_audio", "Edit audio"),
)


# ── Coercion ─────────────────────────────────────────────────────────────────

_KIND_NOUNS = {"INT": "whole number", "FLOAT": "number", "BOOLEAN": "true or false value"}


def _empty(kind: str) -> ControlValueError:
    return ControlValueError(f"The value is empty. Set a {_KIND_NOUNS.get(kind, 'value')}.")


def coerce_control_value(kind: str, raw: Any, *, seed: Any = None) -> Any:
    """Convert a stored Control value to the type its kind delivers.

    Raises ``ControlValueError`` rather than guessing: a Control must never feed
    a silently wrong value, because ComfyUI does not check a linked input.
    """
    if kind == "SEED":
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ControlValueError("No seed is set.")
        return seed
    if kind == "STRING":
        if raw is None:
            return ""
        if not isinstance(raw, str):
            raise ControlValueError(f"Expected text, got {type(raw).__name__}.")
        return raw
    if kind == "COMBO":
        if not isinstance(raw, str):
            raise ControlValueError(f"Expected an option name, got {type(raw).__name__}.")
        return raw
    if kind == "BOOLEAN":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int) and raw in (0, 1):
            return bool(raw)
        if isinstance(raw, str):
            word = raw.strip().lower()
            if not word:
                raise _empty(kind)
            if word in _TRUE_WORDS:
                return True
            if word in _FALSE_WORDS:
                return False
        raise ControlValueError(f"{raw!r} is not true or false.")
    if kind == "INT":
        if isinstance(raw, bool):
            raise ControlValueError(f"{raw!r} is not a whole number.")
        if isinstance(raw, int):
            return raw
        text = str(raw).strip() if isinstance(raw, (str, float)) else None
        if text is None:
            raise ControlValueError(f"{raw!r} is not a whole number.")
        if not text:
            raise _empty(kind)
        if len(text) > _NUMBER_TEXT_LIMIT:
            raise ControlValueError("The value is too long to be a whole number.")
        try:
            number = Decimal(text)
        except InvalidOperation:
            raise ControlValueError(f"{raw!r} is not a whole number.") from None
        if number.is_finite() and number.adjusted() > _INT_EXPONENT_LIMIT:
            raise ControlValueError(f"{raw!r} is too large.")
        if not number.is_finite() or number != number.to_integral_value():
            raise ControlValueError(f"{raw!r} is not a whole number.")
        return int(number)
    if kind == "FLOAT":
        if isinstance(raw, bool):
            raise ControlValueError(f"{raw!r} is not a number.")
        if isinstance(raw, (int, float)):
            number = float(raw)
        elif isinstance(raw, str):
            if not raw.strip():
                raise _empty(kind)
            if len(raw.strip()) > _NUMBER_TEXT_LIMIT:
                raise ControlValueError("The value is too long to be a number.")
            try:
                number = float(raw.strip())
            except ValueError:
                raise ControlValueError(f"{raw!r} is not a number.") from None
        else:
            raise ControlValueError(f"{raw!r} is not a number.")
        if not math.isfinite(number):
            raise ControlValueError(f"{raw!r} is not a finite number.")
        return number
    raise ControlValueError(f"Unknown kind {kind!r}.")


def check_value_for_target(value: Any, spec: dict) -> None:
    """Raise when a target input would refuse this value as a literal.

    ComfyUI checks min, max and COMBO membership only for literals; a linked
    input skips all three, so the Control applies them itself.
    """
    kind = spec.get("kind")
    if kind in ("INT", "SEED", "FLOAT") and isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum, maximum = spec.get("min"), spec.get("max")
        if minimum is not None and value < minimum:
            raise ControlValueError(f"{value} is below the minimum of {minimum}.")
        if maximum is not None and value > maximum:
            raise ControlValueError(f"{value} is above the maximum of {maximum}.")
    if kind == "COMBO":
        options = spec.get("options")
        if isinstance(options, list) and value not in options:
            raise ControlValueError(f"{value!r} is not one of the options.")


MAX_CONTROL_CHANGES = 64
SEED_MAX = 0xFFFFFFFFFFFFFFFF


def validate_control_changes(view: dict, changes: Any) -> tuple[dict, list[dict]]:
    """Whitelist and coerce panel writes against the current descriptor view.

    Returns canonical values, or target/reason refusals. Callers reject the whole
    batch on any refusal. The relay can pass JSON-safe bounds; SDK callers keep
    the full COMBO options and exact Python integers.
    """
    if not isinstance(changes, dict) or not changes or len(changes) > MAX_CONTROL_CHANGES:
        return {}, [{"target": "", "reason": "Send 1 to 64 changes."}]
    targets = {}
    ambiguous = set()

    def add(target, descriptor, field):
        if target in targets:
            ambiguous.add(target)
        targets[target] = (descriptor, field)

    for control in view.get("controls", []):
        identity = control.get("control_id")
        if not identity or control.get("status") in (STATUS_DUPLICATE, STATUS_MISSING_ID):
            continue
        base = f"control:{identity}"
        add(f"{base}:note", control, "note")
        if control.get("status") not in (STATUS_OK, STATUS_UNLINKED):
            continue
        if control.get("kind") == "SEED":
            add(f"{base}:seed", control, "seed")
            add(f"{base}:seed_mode", control, "seed_mode")
        elif control.get("kind") in CONTROL_KINDS:
            add(f"{base}:value", control, "value")
    for note in view.get("notes", []):
        if note.get("note_id") and note.get("status") == STATUS_OK:
            add(f"note:{note['note_id']}:text", note, "text")
    fields = {}
    for descriptor in view.get("fields", []):
        target = f"field:{descriptor.get('class_type')}/{descriptor.get('field')}"
        fields.setdefault(target, []).append(descriptor)
    for target, descriptors in fields.items():
        # A merged field writes every exposed instance, never a linked one.
        if all(d.get("status") == STATUS_OK for d in descriptors):
            targets[target] = (descriptors, "field")

    cleaned, rejected = {}, []
    for target, raw in changes.items():
        try:
            if target not in targets or target in ambiguous:
                raise ControlValueError("This target is unavailable. Refresh the Controls panel.")
            descriptor, field = targets[target]
            if field in ("note", "text"):
                if descriptor.get("note_truncated"):
                    raise ControlValueError("Edit this note on the canvas.")
                if not isinstance(raw, str) or len(raw) > PANEL_NOTE_MAX_CHARS:
                    raise ControlValueError("A note must be text of at most 4000 characters.")
                value = raw
            elif field == "seed_mode":
                if not isinstance(raw, str) or raw not in SEED_MODES:
                    raise ControlValueError("Choose fixed, increment, decrement or randomize.")
                value = raw
            else:
                specs = descriptor if field == "field" else [descriptor]
                kind = "INT" if field == "seed" else specs[0].get("kind")
                value = coerce_control_value(kind, raw)
                if field == "seed" and not 0 <= value <= SEED_MAX:
                    raise ControlValueError("The seed must be between 0 and 18446744073709551615.")
                for spec in specs:
                    if spec.get("kind") != specs[0].get("kind"):
                        raise ControlValueError("These fields have different types.")
                    bounded = dict(spec)
                    for name in ("min", "max"):
                        if isinstance(bounded.get(name), str):
                            bounded[name] = coerce_control_value(kind, bounded[name])
                    # A capped relay view cannot prove membership beyond its
                    # preview; its canvas writer validates against the full view.
                    if bounded.get("options_truncated"):
                        bounded.pop("options", None)
                    check_value_for_target(value, bounded)
            cleaned[target] = value
        except (ControlValueError, OverflowError) as exc:
            rejected.append({"target": target, "reason": str(exc)})
    return cleaned, rejected


# ── Kinds ────────────────────────────────────────────────────────────────────

def derive_kind(target_kinds: Iterable[str]) -> str | None:
    """The kind a Control should have for these targets.

    ``ANY_KIND`` when every target accepts anything, ``None`` when the targets
    disagree. INT and SEED targets agree, and SEED wins when one is present.
    """
    kinds = {kind for kind in target_kinds if kind != ANY_KIND}
    if not kinds:
        return ANY_KIND
    if kinds <= {"INT", "SEED"}:
        return "SEED" if "SEED" in kinds else "INT"
    if len(kinds) == 1:
        return next(iter(kinds))
    return None


def kind_compatible(declared: str, target_kind: str) -> bool:
    """Whether a Control of ``declared`` kind may feed a ``target_kind`` input."""
    if declared not in CONTROL_KINDS:
        return False
    if target_kind == ANY_KIND:
        return True
    if target_kind in ("INT", "SEED"):
        return declared in ("INT", "SEED")
    return declared == target_kind


# The frontend gives these INT inputs a seed control even without the flag.
_SEED_INPUT_NAMES = frozenset({"seed", "noise_seed"})


def spec_from_input_info(input_type: Any, extra: Any, input_name: str = "") -> dict:
    """Reduce one ``INPUT_TYPES`` entry to the facts a Control needs.

    Handles both COMBO shapes (a list of options, or ``"COMBO"`` with
    ``options``). ``kind`` is None for an input that does not take a value. An
    INT is a SEED when the frontend would give it a seed control.
    """
    extra = extra if isinstance(extra, dict) else {}
    spec: dict[str, Any] = {"type": input_type if isinstance(input_type, str) else "COMBO"}
    if isinstance(input_type, (list, tuple)):
        spec.update(kind="COMBO", options=list(input_type))
    elif input_type == "COMBO":
        options = extra.get("options")
        spec.update(kind="COMBO", options=list(options) if isinstance(options, (list, tuple)) else [])
    elif input_type == "INT":
        # The frontend's `control_after_generate ?? isSeedName`: an explicit
        # value wins, so `False` on an input named "seed" means no seed control.
        flag = extra.get("control_after_generate")
        seeded = bool(flag) if flag is not None else input_name in _SEED_INPUT_NAMES
        spec["kind"] = "SEED" if seeded else "INT"
    elif input_type in ("FLOAT", "BOOLEAN", "STRING"):
        spec["kind"] = input_type
    elif input_type in _WILDCARD_TYPES:
        spec["kind"] = ANY_KIND
    else:
        spec["kind"] = None
    for key in ("min", "max", "step", "default", "multiline", "label_on", "label_off"):
        if key in extra and extra[key] is not None:
            spec[key] = extra[key]
    return spec


# ── ComfyUI access (lazy, injectable) ────────────────────────────────────────

def _comfy_class_lookup(class_type: str):
    import nodes  # ComfyUI's nodes module, present inside a running server

    return nodes.NODE_CLASS_MAPPINGS.get(class_type)


# Public name for other modules (template validation) that need the same lookup.
comfy_class_lookup = _comfy_class_lookup


def _is_v3_class(class_def) -> bool:
    try:
        from comfy_api.internal import _ComfyNodeInternal
    except ImportError:
        return False
    return isinstance(class_def, type) and issubclass(class_def, _ComfyNodeInternal)


def _class_inputs(class_def, target_inputs: dict, cache: dict) -> dict:
    """``INPUT_TYPES()`` for a class, finalized for its dynamic inputs (V3)."""
    base = cache.get(class_def)
    if base is None:
        base = class_def.INPUT_TYPES()
        cache[class_def] = base
    if _is_v3_class(class_def):
        try:
            from comfy_api.latest import _io

            finalized, _, _ = _io.get_finalized_class_inputs(base, target_inputs)
            return finalized
        except Exception:
            logger.debug("Could not finalize V3 inputs for %s", class_def, exc_info=True)
    return base


def _input_info(class_inputs: dict, input_name: str) -> tuple[Any, dict] | None:
    for category in ("required", "optional", "hidden"):
        entries = class_inputs.get(category) if isinstance(class_inputs, dict) else None
        if isinstance(entries, dict) and input_name in entries:
            info = entries[input_name]
            if isinstance(info, (list, tuple)) and info:
                extra = info[1] if len(info) > 1 and isinstance(info[1], dict) else {}
                return info[0], extra
            return None
    return None


def _validator_for(class_def) -> tuple[str, Callable] | None:
    if _is_v3_class(class_def):
        try:
            from comfy_api.internal import first_real_override
        except ImportError:
            return None
        function = first_real_override(class_def, "validate_inputs")
        return ("validate_inputs", function) if function is not None else None
    function = getattr(class_def, "VALIDATE_INPUTS", None)
    return ("VALIDATE_INPUTS", function) if function is not None else None


def validator_covers(class_def, input_name: str) -> bool:
    """Whether the class's input validator receives this input at ``/prompt``."""
    found = _validator_for(class_def)
    if found is None:
        return False
    spec = inspect.getfullargspec(found[1])
    return input_name in spec.args or spec.varkw is not None


def _literal_for_validation(value: Any, input_type: Any) -> Any:
    """Apply the cast ``/prompt`` applies to a literal before validators run."""
    if isinstance(value, dict) and "__value__" in value:
        value = value["__value__"]
    casts = {"INT": int, "FLOAT": float, "STRING": str, "BOOLEAN": bool}
    cast = casts.get(input_type) if isinstance(input_type, str) else None
    if cast is not None:
        try:
            return cast(value)
        except Exception:
            return value
    return value


async def _comfy_dry_run(class_def, key: str, inputs: dict, received_types: dict) -> tuple[bool, str | None]:
    """Call the target's validator the way ``execution.validate_inputs`` does.

    Every linked input reaches the validator as ``None`` at ``/prompt`` (there is
    no execution list yet), so this reproduces the call exactly by using
    ComfyUI's own ``get_input_data`` and node-mapping helpers.
    """
    import execution
    from comfy_execution.graph import ExecutionBlocker

    found = _validator_for(class_def)
    if found is None:
        return True, None
    function_name, function = found
    spec = inspect.getfullargspec(function)
    if not spec.args and spec.varkw is None:
        return True, None
    input_data, _, v3_data = execution.get_input_data(inputs, class_def, key)
    filtered = {
        name: value
        for name, value in input_data.items()
        if name in spec.args or spec.varkw is not None
    }
    if "input_types" in spec.args:
        filtered["input_types"] = [received_types]
    results = await execution._async_map_node_over_list(
        None, key, class_def, filtered, function_name, v3_data=v3_data
    )
    results = await execution.resolve_map_node_over_list_results(results)
    for result in results:
        if result is not True and not isinstance(result, ExecutionBlocker):
            return False, None if result is False else str(result)
    return True, None


def comfy_validator_runner(requests: list[dict]) -> list[tuple[bool, str | None]]:
    """Run dry-run requests in a private event loop (call off the server loop)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("describe_controls blocks; call it through asyncio.to_thread.")

    async def run_all():
        outcomes = []
        for request in requests:
            try:
                outcomes.append(await _comfy_dry_run(
                    request["class_def"], request["key"], request["inputs"], request["received_types"]
                ))
            except Exception as exc:  # the validator itself raised, as at /prompt
                outcomes.append((False, f"{type(exc).__name__}: {exc}"))
        return outcomes

    return asyncio.run(run_all())


# ── Describe ─────────────────────────────────────────────────────────────────

def _control_targets(prompt: dict, control_key: str) -> list[tuple[str, str]]:
    targets = []
    for key, node in prompt.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for input_name, value in inputs.items():
            if prompt_graph.linked_node_id(value) == control_key:
                targets.append((str(key), str(input_name)))
    return targets


def _node_title(node: dict, default: str = DEFAULT_CONTROL_LABEL) -> str:
    meta = node.get("_meta")
    title = meta.get("title") if isinstance(meta, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else default


def _received_types(prompt: dict, inputs: dict, class_lookup: Callable) -> dict:
    received = {}
    for name, value in inputs.items():
        source = prompt_graph.linked_node_id(value)
        if source is None or not isinstance(prompt.get(source), dict):
            continue
        source_class = class_lookup(prompt[source].get("class_type"))
        try:
            received[name] = source_class.RETURN_TYPES[int(value[1])]
        except Exception:
            continue
    return received


def _read_target(prompt: dict, target_key: str, input_name: str, class_lookup: Callable, cache: dict) -> dict:
    node = prompt.get(target_key) or {}
    class_type = node.get("class_type")
    target = {"key": target_key, "class_type": class_type, "input": input_name}
    class_def = class_lookup(class_type) if isinstance(class_type, str) else None
    if class_def is None:
        target.update(status=STATUS_UNSUPPORTED, kind=None, reason=f"{class_type} is not installed.")
        return target
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    try:
        info = _input_info(_class_inputs(class_def, inputs, cache), input_name)
    except Exception as exc:
        logger.debug("Could not read inputs of %s", class_type, exc_info=True)
        info = None
        target["reason"] = f"Its inputs could not be read ({type(exc).__name__})."
    if info is None:
        target.update(status=STATUS_UNSUPPORTED, kind=None)
        target.setdefault("reason", f"{class_type} has no input named {input_name}.")
        return target
    spec = spec_from_input_info(*info, input_name)
    target.update(spec)
    target["class_def"] = class_def
    if spec["kind"] is None:
        target.update(status=STATUS_UNSUPPORTED, reason=f"It takes {spec['type']}, not a value.")
    else:
        target["status"] = STATUS_OK
    return target


def _merge_bounds(targets: list[dict]) -> dict:
    merged: dict[str, Any] = {}
    numeric = [t for t in targets if t.get("kind") in ("INT", "SEED", "FLOAT")]
    mins = [t["min"] for t in numeric if isinstance(t.get("min"), (int, float))]
    maxs = [t["max"] for t in numeric if isinstance(t.get("max"), (int, float))]
    if mins:
        merged["min"] = max(mins)
    if maxs:
        merged["max"] = min(maxs)
    steps = [t["step"] for t in numeric if isinstance(t.get("step"), (int, float))]
    if steps:
        merged["step"] = steps[0]
    combos = [t["options"] for t in targets if t.get("kind") == "COMBO" and isinstance(t.get("options"), list)]
    if combos:
        merged["options"] = [option for option in combos[0] if all(option in other for other in combos[1:])]
    if any(t.get("multiline") for t in targets):
        merged["multiline"] = True
    return merged


def _public_target(target: dict) -> dict:
    keys = ("key", "class_type", "input", "kind", "status", "reason", "detail")
    return {key: target[key] for key in keys if key in target}


def _describe_one_control(prompt: dict, key: str, node: dict, class_lookup: Callable, cache: dict) -> tuple[dict, list[dict]]:
    inputs = prompt_graph.node_inputs(prompt, key)
    declared = inputs.get("kind")
    declared = declared if isinstance(declared, str) else ""
    seed = inputs.get("seed")
    targets = [
        _read_target(prompt, target_key, input_name, class_lookup, cache)
        for target_key, input_name in _control_targets(prompt, key)
    ]
    order = inputs.get("panel_order")
    group = inputs.get("panel_group")
    descriptor: dict[str, Any] = {
        "source": "control",
        "key": key,
        "keys": [key],
        "control_id": inputs.get("control_id") if isinstance(inputs.get("control_id"), str) else "",
        "label": _node_title(node),
        "kind": declared,
        "value_text": inputs.get("value_text") if isinstance(inputs.get("value_text"), str) else "",
        "seed": seed if isinstance(seed, int) and not isinstance(seed, bool) else None,
        "seed_mode": inputs.get("seed_mode") if inputs.get("seed_mode") in SEED_MODES else None,
        "note": inputs.get("panel_note") if isinstance(inputs.get("panel_note"), str) else "",
        "order": order if isinstance(order, int) and not isinstance(order, bool) else 0,
        "group": group.strip() if isinstance(group, str) else "",
    }
    return descriptor, targets


def _finish_control(descriptor: dict, targets: list[dict], pending_reasons: dict) -> dict:
    declared = descriptor["kind"]
    usable = [t for t in targets if t.get("status") == STATUS_OK]
    target_kind = derive_kind(t["kind"] for t in usable)
    descriptor["target_kind"] = target_kind
    descriptor.update(_merge_bounds(usable))
    for target in targets:
        refusal = pending_reasons.get((target["key"], target["input"]))
        if refusal is not None:
            target["status"] = STATUS_UNSUPPORTED
            target["reason"] = VALIDATOR_REFUSAL
            if refusal:
                target["detail"] = refusal
    if not targets:
        status = STATUS_UNLINKED
    elif any(t.get("status") == STATUS_UNSUPPORTED for t in targets):
        status = STATUS_UNSUPPORTED
    elif target_kind is None:
        status = STATUS_MIXED
    elif not all(kind_compatible(declared, t["kind"]) for t in usable):
        status = STATUS_STALE
    else:
        status = STATUS_OK
    descriptor["status"] = status
    descriptor["targets"] = [_public_target(t) for t in targets]
    descriptor["value"] = None
    descriptor["error"] = None
    if declared in CONTROL_KINDS:
        try:
            value = coerce_control_value(declared, descriptor["value_text"], seed=descriptor["seed"])
            for target in usable:
                check_value_for_target(value, target)
            descriptor["value"] = value
        except ControlValueError as exc:
            descriptor["error"] = str(exc)
    return descriptor


def _group_duplicates(controls: list[dict]) -> list[dict]:
    """One descriptor per ``control_id``: two instances of one subgraph
    definition carry the same inner Control and drive it together."""
    grouped: dict[str, dict] = {}
    result = []
    for descriptor in controls:
        control_id = descriptor["control_id"]
        if not control_id:
            descriptor["status"] = STATUS_MISSING_ID
            descriptor["error"] = "This Control has no id. Open the workflow on the canvas to assign one."
        first = grouped.get(control_id) if control_id else None
        if first is None:
            if control_id:
                grouped[control_id] = descriptor
            result.append(descriptor)
            continue
        first["keys"].append(descriptor["key"])
        first["targets"].extend(descriptor["targets"])
        same = all(first[name] == descriptor[name] for name in ("kind", "value_text", "seed", "seed_mode", "note", "label", "order", "group"))
        if not same:
            first["status"] = STATUS_DUPLICATE
            first["error"] = "Two different Controls share one id. Delete one and add it again."
        else:
            # Every instance of a shared definition constrains the same value.
            # Preserve all bounds/options and every validation refusal.
            first.update(_merge_bounds([first, descriptor]))
            if descriptor["status"] != STATUS_OK and first["status"] == STATUS_OK:
                first["status"] = descriptor["status"]
            if descriptor.get("error") and not first.get("error"):
                first["error"] = descriptor["error"]
            if first.get("value") is not None:
                try:
                    check_value_for_target(first["value"], first)
                except ControlValueError as exc:
                    first["error"] = str(exc)
    return result


def _truncate_note(descriptor: dict, field: str) -> None:
    if len(descriptor[field]) > PANEL_NOTE_MAX_CHARS:
        descriptor[field] = descriptor[field][:PANEL_NOTE_MAX_CHARS]
        descriptor["note_truncated"] = True


def _describe_notes(prompt: dict) -> list[dict]:
    grouped: dict[str, dict] = {}
    notes = []
    for key, node in prompt.items():
        if not isinstance(node, dict) or node.get("class_type") != PANEL_NOTE_CLASS_TYPE:
            continue
        inputs = prompt_graph.node_inputs(prompt, key)
        note_id = inputs.get("note_id") if isinstance(inputs.get("note_id"), str) else ""
        order, group = inputs.get("panel_order"), inputs.get("panel_group")
        descriptor = {
            "source": "note", "key": str(key), "keys": [str(key)], "note_id": note_id,
            "label": _node_title(node, "Sonder Panel Note"),
            "text": inputs.get("text") if isinstance(inputs.get("text"), str) else "",
            "order": order if isinstance(order, int) and not isinstance(order, bool) else 0,
            "group": group.strip() if isinstance(group, str) else "",
            "status": STATUS_OK if note_id else STATUS_MISSING_ID,
            "reason": None if note_id else "This note has no id. Open the workflow on the canvas to assign one.",
        }
        first = grouped.get(note_id) if note_id else None
        if first is None:
            notes.append(descriptor)
            if note_id:
                grouped[note_id] = descriptor
        else:
            first["keys"].append(str(key))
            if any(first[name] != descriptor[name] for name in ("text", "label", "order", "group")):
                first.update(status=STATUS_DUPLICATE,
                             reason="Two different Panel Notes share one id. Delete one and add it again.")
    # Compare full authored text before truncating so different tails never merge.
    for descriptor in notes:
        _truncate_note(descriptor, "text")
    notes.sort(key=lambda d: (d["order"], d["label"].lower(), d["key"]))
    return notes


def json_safe(view: dict) -> dict:
    """Copy a descriptor view at the HTTP edge, preserving integer precision.

    Internal consumers keep exact numbers and complete option lists for validation.
    The transport preview limits never change their source or a submitted prompt.
    """
    def copy(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            result = {}
            for name, item in value.items():
                integer_field = value.get("kind") in ("INT", "SEED") and name in (
                    "seed", "value", "min", "max", "step", "default")
                integer_field = integer_field or name == "seed" or (
                    value.get("source") == "field" and name in ("default", "min", "max")
                    and isinstance(item, int))
                # Coerced relay changes retain exact integers on the wire too.
                integer_field = integer_field or (isinstance(item, int) and not isinstance(item, bool)
                    and isinstance(name, str) and (name.startswith("field:") or
                    (name.startswith("control:") and name.endswith((":value", ":seed")))))
                if isinstance(item, float) and not math.isfinite(item):
                    result[name] = None
                elif integer_field and isinstance(item, (int, float)) and not isinstance(item, bool):
                    result[name] = str(int(item)) if isinstance(item, int) or item.is_integer() else str(item)
                elif name == "options" and isinstance(item, (list, tuple)):
                    result[name] = [copy(option) for option in item[:PANEL_OPTIONS_LIMIT]]
                    if len(item) > PANEL_OPTIONS_LIMIT:
                        result["options_truncated"] = True
                else:
                    result[name] = copy(item)
            return result
        if isinstance(value, (list, tuple)):
            return [copy(item) for item in value]
        return value

    return copy(view)


def _describe_fields(prompt: dict, class_lookup: Callable, cache: dict) -> list[dict]:
    fields = []
    by_class: dict[str, list[FieldControl]] = {}
    for entry in FIELD_CONTROLS:
        by_class.setdefault(entry.class_type, []).append(entry)
    seen = set()
    for editor_key in prompt_graph.editor_keys(prompt):
        for key in prompt_graph.nodes_reaching(prompt, editor_key, by_class):
            node = prompt[key]
            inputs = prompt_graph.node_inputs(prompt, key)
            class_def = class_lookup(node.get("class_type"))
            for entry in by_class[node.get("class_type")]:
                if (key, entry.field) in seen:
                    continue
                seen.add((key, entry.field))
                raw = inputs.get(entry.field)
                source = prompt_graph.linked_node_id(raw)
                if source is not None and (prompt.get(source) or {}).get("class_type") == CONTROL_CLASS_TYPE:
                    continue  # the Control's own descriptor drives this field
                info = None
                if class_def is not None:
                    try:
                        info = _input_info(_class_inputs(class_def, inputs, cache), entry.field)
                    except Exception:
                        info = None
                spec = spec_from_input_info(*info, entry.field) if info else {"kind": None}
                descriptor = {
                    "source": "field",
                    "key": key,
                    "class_type": node.get("class_type"),
                    "field": entry.field,
                    "label": entry.label,
                    "editor_key": editor_key,
                    "kind": spec.get("kind"),
                    "value": None,
                    "status": STATUS_OK,
                    "reason": None,
                }
                for name in ("label_on", "label_off", "default", "options", "min", "max"):
                    if name in spec:
                        descriptor[name] = spec[name]
                if spec.get("kind") not in CONTROL_KINDS:
                    descriptor.update(status=STATUS_UNSUPPORTED, reason="This field could not be read.")
                elif source is not None:
                    descriptor.update(status=STATUS_UNSUPPORTED, reason="Another node sets this field.")
                else:
                    try:
                        descriptor["value"] = coerce_control_value(spec["kind"], raw)
                    except ControlValueError as exc:
                        descriptor.update(status=STATUS_UNSUPPORTED, reason=f"Its value can't be read. {exc}")
                fields.append(descriptor)
    return fields


_NO_LITERAL = object()


def _dry_run_request(prompt: dict, target: dict, class_lookup: Callable, cache: dict, *, literal: Any = _NO_LITERAL) -> dict:
    """A validator call for ``target``'s node: literals cast as /prompt casts
    them, links kept (they reach the validator as None). With ``literal``, the
    Control's link on the target input is replaced by that value."""
    class_def = target["class_def"]
    node_inputs = dict(prompt_graph.node_inputs(prompt, target["key"]))
    if literal is not _NO_LITERAL:
        node_inputs[target["input"]] = literal
    class_inputs = _class_inputs(class_def, node_inputs, cache)
    inputs = {}
    for name, value in node_inputs.items():
        info = _input_info(class_inputs, name)
        inputs[name] = value if prompt_graph.linked_node_id(value) else _literal_for_validation(
            value, info[0] if info else None
        )
    return {
        "class_def": class_def,
        "key": target["key"],
        "inputs": inputs,
        "received_types": _received_types(prompt, node_inputs, class_lookup),
    }


def _baseline_literals(descriptor: dict, target: dict) -> list:
    """Literals that could stand where the Control's link is: its own value,
    then the input's default, then its first option."""
    candidates = []
    try:
        candidates.append(coerce_control_value(descriptor["kind"], descriptor["value_text"], seed=descriptor["seed"]))
    except ControlValueError:
        pass
    candidates.append(target.get("default"))
    options = target.get("options")
    if isinstance(options, list) and options:
        candidates.append(options[0])
    unique = []
    for value in candidates:
        if value is not None and not isinstance(value, (list, dict)) and value not in unique:
            unique.append(value)
    return unique


def describe_controls(
    prompt: Any,
    *,
    class_lookup: Callable[[str], Any] | None = None,
    validator_runner: Callable[[list[dict]], list[tuple[bool, str | None]]] | None = None,
) -> dict:
    """Describe every Sonder Control and registry field in an API prompt.

    Blocking: it may call target validators. Run it off the event loop.
    """
    if not isinstance(prompt, dict):
        raise ValueError("A prompt must be an object of nodes.")
    class_lookup = class_lookup or _comfy_class_lookup
    validator_runner = validator_runner or comfy_validator_runner
    cache: dict = {}

    pending = []
    for key, node in prompt.items():
        if isinstance(node, dict) and node.get("class_type") == CONTROL_CLASS_TYPE:
            pending.append(_describe_one_control(prompt, str(key), node, class_lookup, cache))

    # Dry-run each covered target's validator with every link as None, exactly
    # as /prompt will. A failure is blamed on the Control only when the same
    # call passes with a literal in place of its link; otherwise the node fails
    # for its own reasons and /prompt will say so.
    requests, failing_candidates = [], []
    for descriptor, targets in pending:
        for target in targets:
            class_def = target.get("class_def")
            if target.get("status") != STATUS_OK or class_def is None:
                continue
            if not validator_covers(class_def, target["input"]):
                continue
            requests.append(_dry_run_request(prompt, target, class_lookup, cache))
            failing_candidates.append((target, _baseline_literals(descriptor, target)))
    reasons: dict[tuple[str, str], str] = {}
    if requests:
        failed = [
            (target, candidates, message or "")
            for (target, candidates), (ok, message) in zip(failing_candidates, validator_runner(requests))
            if not ok
        ]
        baselines, owners = [], []
        for index, (target, candidates, _) in enumerate(failed):
            for literal in candidates:
                baselines.append(_dry_run_request(prompt, target, class_lookup, cache, literal=literal))
                owners.append(index)
        passing = set()
        if baselines:
            for index, (ok, _) in zip(owners, validator_runner(baselines)):
                if ok:
                    passing.add(index)
        for index, (target, _, message) in enumerate(failed):
            if index in passing:
                reasons[(target["key"], target["input"])] = message

    controls = [_finish_control(descriptor, targets, reasons) for descriptor, targets in pending]
    controls = _group_duplicates(controls)
    for descriptor in controls:
        _truncate_note(descriptor, "note")
    controls.sort(key=lambda d: (d["order"], d["label"].lower(), d["key"]))
    return {
        "controls": controls,
        "notes": _describe_notes(prompt),
        "fields": _describe_fields(prompt, class_lookup, cache),
        "editors": prompt_graph.editor_keys(prompt),
    }


# ── Execute-time self-check ──────────────────────────────────────────────────

def resolve_control_value(
    prompt: Any,
    unique_id: Any,
    *,
    kind: str,
    value_text: Any,
    seed: Any,
    label: str | None = None,
    class_lookup: Callable[[str], Any] | None = None,
) -> Any:
    """The typed value a Control delivers, checked against every input it feeds.

    Raises ``ControlValueError`` with a message naming the Control and target.
    Validators are not dry-run here: ``/prompt`` already ran them.
    """
    class_lookup = class_lookup or _comfy_class_lookup
    key = str(unique_id) if unique_id is not None else ""
    name = label
    if name is None:
        node = prompt.get(key) if isinstance(prompt, dict) else None
        name = _node_title(node) if isinstance(node, dict) else DEFAULT_CONTROL_LABEL

    def fail(message: str) -> ControlValueError:
        return ControlValueError(f"Sonder Control '{name}': {message}")

    targets = []
    if isinstance(prompt, dict) and key in prompt:
        cache: dict = {}
        targets = [
            _read_target(prompt, target_key, input_name, class_lookup, cache)
            for target_key, input_name in _control_targets(prompt, key)
        ]
    for target in targets:
        where = f"{target['class_type']} {target['input']}"
        if target["status"] != STATUS_OK:
            raise fail(f"It cannot feed {where}. {target.get('reason') or ''}".rstrip())
        if not kind_compatible(kind, target["kind"]):
            raise fail(
                f"It is set up for {kind or 'no type'} but feeds {where}, which takes {target['kind']}. "
                "Reconnect the Control to update its type."
            )
    try:
        value = coerce_control_value(kind, value_text, seed=seed)
    except ControlValueError as exc:
        raise fail(str(exc)) from None
    for target in targets:
        try:
            check_value_for_target(value, target)
        except ControlValueError as exc:
            raise fail(f"{str(exc).rstrip('.')} for {target['class_type']} {target['input']}.") from None
    return value


# ── Seeds ────────────────────────────────────────────────────────────────────

def next_seed_value(
    mode: str,
    value: Any,
    *,
    minimum: Any = None,
    maximum: Any = None,
    step: Any = None,
    rng: random.Random | None = None,
) -> Any:
    """The seed after one accepted submit, as the frontend 1.53.10 computes it.

    A transliteration of ``computeNextNumberValue``: bounds default to 0..1 and
    are clamped to +-2^50, ``increment-wrap`` acts as ``increment`` for numbers,
    and ``fixed`` (or an unknown mode, or a non-number) leaves the value alone.
    """
    if mode not in ("increment", "increment-wrap", "decrement", "randomize"):
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    lo = max(-_SEED_LIMIT, 0 if minimum is None else minimum)
    hi = min(_SEED_LIMIT, 1 if maximum is None else maximum)
    step = 1 if step is None else step
    span = (hi - lo) / step
    if mode in ("increment", "increment-wrap"):
        result = value + step
    elif mode == "decrement":
        result = value - step
    else:
        result = math.floor((rng or random).random() * span) * step + lo
    result = min(max(result, lo), hi)
    if isinstance(value, int) and isinstance(step, int) and isinstance(lo, int):
        return int(result)
    return result
