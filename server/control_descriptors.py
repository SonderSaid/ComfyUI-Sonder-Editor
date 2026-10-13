"""Controls descriptors and the one coercion authority.

``describe_bindings(prompt)`` reads an API-format prompt and returns what the
Controls panel shows: one descriptor per binding of the workflow's Sonder
Control Relay (its kind, value, bounds, options and the inputs it sets), plus
the Relay's notes. It reads the *graph*, never a project, so the
change-certificate rule does not apply to it.

The Relay's exported manifest names each binding's targets. They are never
trusted: every target is checked against the prompt here (present, a literal,
declared by its node, not owned by Sonder). Bindings never link into an input,
so no validator dry run is needed: a value is a literal, and ComfyUI validates
literals at ``/prompt``.

``coerce_control_value`` and ``check_value_for_target`` are the only conversion
and validation rules for a Control value. The panel, the mounted relay and the
SDK's stored values all go through them.

The module imports only the standard library and sibling pure modules at module
level. ComfyUI's ``nodes`` and ``comfy_api`` are imported lazily, inside a
running ComfyUI, and tests inject fakes instead.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import logging
import math
import random
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from . import binding_policy, control_manifest, prompt_graph

logger = logging.getLogger(__name__)

PANEL_NOTE_MAX_CHARS = control_manifest.NOTE_MAX
PANEL_LABEL_MAX_CHARS = control_manifest.LABEL_MAX
PANEL_OPTIONS_LIMIT = 2000
CONTROL_KINDS = ("STRING", "INT", "FLOAT", "BOOLEAN", "COMBO", "SEED")
ANY_KIND = "*"
# Input types that accept any value, so they add no kind of their own.
_WILDCARD_TYPES = frozenset({"*", "COMFY_MATCHTYPE_V3"})

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_DUPLICATE = "duplicate"
STATUS_LINKED = "linked"
STATUS_NO_WIDGET = "no_widget"
STATUS_UNSUPPORTED = "unsupported"
STATUS_DENIED = "denied"
# Sonder App only: an app-only binding whose input the workflow now exposes itself.
STATUS_SHADOWED = "shadowed"

STATUS_REASONS = {
    STATUS_SHADOWED: "The workflow now exposes this setting itself, so this app-only Control is off. Remove it.",
    STATUS_MISSING: "This setting is no longer in the workflow, moved into a subgraph, or its node is muted or bypassed.",
    STATUS_DUPLICATE: "Two nodes carry this Control. Remove one of them.",
    STATUS_LINKED: "Another node is connected to this input, so the Control can't set it.",
    STATUS_NO_WIDGET: "This input has no editable value right now.",
    STATUS_UNSUPPORTED: "This setting can't be a Control.",
    STATUS_DENIED: binding_policy.DENIED_REASON,
}

DEFAULT_LABEL = "Setting"

# Mirrors the frontend 1.53.10 number control (`computeNextNumberValue`), which
# keeps randomized values inside +-2^50.
SEED_MODES = control_manifest.SEED_MODES
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
    """A Sonder-node widget Manage recommends exposing.

    ``label`` None names the Control after its node's title (a Selector's title
    says which feature it picks). Its section follows ``section_for``.
    """

    class_type: str
    field: str
    label: str | None
    hint: str = ""


# Entries, not conditionals: add a row to recommend another Sonder-node field.
# Exposure is opt-in (maintainer decisions 2026-10-06 and 2026-10-08: no
# defaults); Manage lists these first as Recommended.
FIELD_CONTROLS: tuple[FieldControl, ...] = (
    # io_nodes.SonderSaveVideo refuses a custom PNG sequence in Take mode.
    FieldControl("SonderSaveVideo", "mode", "Save as", "Take can't save a PNG sequence."),
    FieldControl("SonderSaveVideo", "filename_prefix", "File name"),
    FieldControl("SonderSaveVideo", "save_preset", "Save preset"),
    FieldControl("SonderSaveVideo", "mark_queue_complete", "Advance queue after take"),
    FieldControl("SonderSaveVideo", "place_audio_on_timeline", "Place take audio on timeline"),
    FieldControl("SonderSaveBridge", "prefix", "Prefix"),
    FieldControl("SonderSaveBridge", "mark_queue_complete", "Advance queue after take"),
    FieldControl("SonderMasksBridge", "edit_video", "Edit video"),
    FieldControl("SonderMasksBridge", "edit_audio", "Edit audio"),
    FieldControl("SonderSelector", "choice", None),
)
_RECOMMENDED = {(entry.class_type, entry.field): entry for entry in FIELD_CONTROLS}

# Section by node class (maintainer, 2026-10-08): every exposed setting of these
# nodes belongs to the editor's run, so it shows in the Editor section; any other
# node's setting is a Workflow Control. web/js/control_bindings.js mirrors it.
EDITOR_CLASSES = frozenset({"SonderSaveVideo", "SonderSaveBridge", "SonderMasksBridge"})


def section_for(prompt: Any, targets: Any, fallback: str = "workflow") -> str:
    """``"editor"`` when every target's node is an editor class, else ``"workflow"``.

    The class comes from the prompt node at each target path, so a linked or
    widgetless target keeps its section. ``fallback`` (the stored section) is
    used only when a target's node is gone.
    """
    classes = []
    for target in targets or []:
        node = prompt.get(target.get("path")) if isinstance(prompt, dict) and isinstance(target, dict) else None
        if not isinstance(node, dict):
            return fallback if fallback in control_manifest.SECTIONS else "workflow"
        classes.append(node.get("class_type"))
    if not classes:
        return fallback if fallback in control_manifest.SECTIONS else "workflow"
    return "editor" if all(c in EDITOR_CLASSES for c in classes) else "workflow"


def recommended_label(entry: FieldControl, node: Any) -> str:
    """The Recommended label: the entry's own, or its node's title."""
    if entry.label:
        return entry.label
    return _node_title(node, str(node.get("class_type") or entry.class_type)) if isinstance(node, dict) else entry.class_type


def group_order(view: dict) -> list[str]:
    """The Controls' group order, one authority for the panel and provenance.

    Groups with an entry in the manifest's ``groups`` come first by their order
    (then name); groups only named by Workflow bindings or notes follow in the
    order they first appear, which is how the panel ordered every group before
    Phase 4. An Editor-section binding's group shows nowhere, so it adds none.
    web/js/control_bindings.js ``effectiveGroupOrder`` mirrors it.
    """
    explicit = view.get("groups") if isinstance(view.get("groups"), dict) else {}
    ordered = sorted((name for name in explicit if isinstance(name, str) and name),
                     key=lambda name: (_clean_order((explicit.get(name) or {}).get("order")), name))
    rows = [*(d for d in view.get("bindings") or [] if d.get("section") != "editor"), *(view.get("notes") or [])]
    rows.sort(key=lambda d: (_clean_order(d.get("order")), str(d.get("label") or "").lower(),
                             str(d.get("binding_id") or d.get("note_id") or "")))
    for row in rows:
        name = row.get("group")
        if isinstance(name, str) and name and name not in ordered:
            ordered.append(name)
    return ordered

# ── Coercion ─────────────────────────────────────────────────────────────────

_KIND_NOUNS = {"INT": "whole number", "FLOAT": "number", "BOOLEAN": "true or false value"}


def _empty(kind: str) -> ControlValueError:
    return ControlValueError(f"The value is empty. Set a {_KIND_NOUNS.get(kind, 'value')}.")


def coerce_control_value(kind: str, raw: Any, *, seed: Any = None) -> Any:
    """Convert a stored Control value to the type its kind delivers.

    Raises ``ControlValueError`` rather than guessing.
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

    The panel and the SDK check before saving, so a refused value never waits
    for ``/prompt`` to fail.
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


PANEL_ORDER_LIMIT = 9999
SEED_MAX = 0xFFFFFFFFFFFFFFFF


def coerce_panel_layout(field: str, raw: Any):
    if field == "order":
        value = coerce_control_value("INT", raw)
        if not -PANEL_ORDER_LIMIT <= value <= PANEL_ORDER_LIMIT:
            raise ControlValueError("Panel order must be between -9999 and 9999.")
        return value
    if not isinstance(raw, str):
        raise ControlValueError("A panel group must be text.")
    # Empty is ungrouped; any other name follows the one group-name rule.
    if raw.strip() and (error := control_manifest.group_name_error(raw)):
        raise ControlValueError(error)
    return raw.strip()


# ── Change targets ───────────────────────────────────────────────────────────
#
# One grammar for every change target, parsed in one place (the JS mirror is
# control_bindings.js ``parseTarget``): ``binding:<id>:<field>``,
# ``note:<id>:<field>``, ``group:<name>:<op>`` (a name holds no ``:``) and the
# two-part ``workflow:description``.
_TARGET_RE = re.compile(r"^(?:(binding|note|preset):([A-Za-z0-9_-]{1,64})|(group):([^:]+)):([a-z_]+)$")
# Manage's structural targets: the validator returns an internal write plan,
# and the mounted relay forwards the original intent for the canvas to recheck.
BINDING_STRUCTURAL_OPS = frozenset({"expose", "remove", "rebind", "hide", "unhide", "create"})
GROUP_OPS = frozenset({"create", "rename", "ungroup", "move"})
STRUCTURAL_OPS = {"binding": BINDING_STRUCTURAL_OPS, "note": frozenset({"create", "remove", "hide", "unhide"}),
                  "group": GROUP_OPS, "preset": frozenset({"create", "capture", "apply"}), "workflow": frozenset({"cleanup", "restore"})}


def parse_target(target: Any) -> tuple[str, str, str] | None:
    """``(source, identity, field)`` of a change target, or None."""
    if not isinstance(target, str):
        return None
    if target in ("workflow:description", "workflow:cleanup", "workflow:restore"):
        return ("workflow", "", target.split(":")[1])
    match = _TARGET_RE.match(target)
    if not match:
        return None
    if match.group(1):
        return (match.group(1), match.group(2), match.group(5))
    return ("group", match.group(4), match.group(5))


def is_structural(target: Any) -> bool:
    parsed = parse_target(target)
    if parsed is None:
        return False
    source, _, field = parsed
    return field in STRUCTURAL_OPS.get(source, ())


# ── Kinds ────────────────────────────────────────────────────────────────────

def derive_kind(target_kinds: Iterable[str]) -> str | None:
    """The kind a binding has for these targets.

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


# The frontend gives these INT inputs a seed control even without the flag.
_SEED_INPUT_NAMES = frozenset({"seed", "noise_seed"})


def spec_from_input_info(input_type: Any, extra: Any, input_name: str = "") -> dict:
    """Reduce one ``INPUT_TYPES`` entry to the facts a Control needs.

    Handles both COMBO shapes (a list of options, or ``"COMBO"`` with
    ``options``). ``kind`` is None for an input that does not take a value. An
    INT is a SEED when the frontend would give it a seed control.
    """
    extra = extra if isinstance(extra, dict) else {}
    # ComfyUI str enums overload comparisons for wildcard compatibility.
    # Descriptors need literal type identity, not socket-connection semantics.
    if isinstance(input_type, str):
        input_type = str(input_type)
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


# ── Targets ──────────────────────────────────────────────────────────────────

def _node_title(node: dict, default: str = DEFAULT_LABEL) -> str:
    meta = node.get("_meta")
    title = meta.get("title") if isinstance(meta, dict) else None
    return title.strip() if isinstance(title, str) and title.strip() else default


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


# ── Describe ─────────────────────────────────────────────────────────────────

def _is_link(value: Any) -> bool:
    """An API-prompt link ``[node_id, slot]``. A multiselect value is a list too."""
    return (isinstance(value, list) and len(value) == 2 and isinstance(value[0], (str, int))
            and not isinstance(value[0], bool) and isinstance(value[1], int) and not isinstance(value[1], bool))


def _default_label(prompt: dict, targets: list[dict]) -> str:
    if not targets:
        return DEFAULT_LABEL
    node = prompt.get(targets[0]["path"])
    title = _node_title(node, str(node.get("class_type") or "")) if isinstance(node, dict) else ""
    return f"{title} · {targets[0]['input']}" if title else targets[0]["input"]


def _effective_inputs(prompt: dict, path: str, overrides: dict | None) -> dict:
    inputs = dict(prompt_graph.node_inputs(prompt, path))
    for (key, name), value in (overrides or {}).items():
        if key == path:
            inputs[name] = value
    return inputs


def _bounds_for(prompt: dict, read: list[dict], overrides: dict | None = None) -> tuple[dict, bool, bool, list | None]:
    """Range and options from the targets whose checks Sonder can apply itself.

    Per target: an options provider supplies options from the node's effective
    inputs; an input a validator covers adds no check (native skips its range
    and option checks, execution.py:1029); any other target adds its declared
    spec. Returns (bounds, any target checked only at run, any provider,
    the static option list when a provider also applies).
    """
    static, dynamic, unknown, provider = [], [], False, False
    for target in read:
        fn = binding_policy.provider_for(target.get("class_type"), target.get("input"))
        if fn is not None:
            provider = True
            options = fn(_effective_inputs(prompt, target["key"], overrides))
            if options is None:
                unknown = True
            else:
                dynamic.append(options)
        elif target.get("class_def") is not None and validator_covers(target["class_def"], target["input"]):
            unknown = True
        else:
            static.append(target)
    merged = _merge_bounds(static)
    merged.pop("multiline", None)
    static_options = merged.pop("options", None)
    lists = ([static_options] if static_options is not None else []) + dynamic
    if lists:
        merged["options"] = [option for option in lists[0] if all(option in other for other in lists[1:])]
    return merged, unknown, provider, static_options if provider else None


def _check_targets(prompt: dict, targets: list[dict], class_lookup: Callable, cache: dict) -> tuple[str, str | None, list[dict]]:
    """Read every exported target from the prompt; return (status, reason, read targets)."""
    if not targets:
        return STATUS_MISSING, None, []
    read = []
    for target in targets:
        path, name = target["path"], target["input"]
        node = prompt.get(path)
        if not isinstance(node, dict):
            read.append({"key": path, "class_type": None, "input": name, "status": STATUS_MISSING})
            continue
        class_type = node.get("class_type")
        # An app-only binding remembers its node's class: a re-import that
        # reuses the id for another node must not inherit the binding.
        if target.get("class_type") is not None and target["class_type"] != class_type:
            read.append({"key": path, "class_type": class_type, "input": name, "status": STATUS_MISSING})
            continue
        value = prompt_graph.node_inputs(prompt, path).get(name)
        if binding_policy.is_denied(class_type, name):
            read.append({"key": path, "class_type": class_type, "input": name, "status": STATUS_DENIED})
            continue
        if _is_link(value):
            read.append({"key": path, "class_type": class_type, "input": name, "status": STATUS_LINKED})
            continue
        entry = _read_target(prompt, path, name, class_lookup, cache)
        if entry["status"] == STATUS_OK and isinstance(value, (list, dict)):
            entry.update(status=STATUS_UNSUPPORTED, reason="Lists of choices can't be Controls yet.")
        if entry["status"] == STATUS_OK and entry.get("kind") == ANY_KIND:
            entry.update(status=STATUS_UNSUPPORTED, reason="This input accepts any type, so it can't be a Control.")
        if entry["status"] == STATUS_OK and name not in prompt_graph.node_inputs(prompt, path):
            entry.update(status=STATUS_MISSING)
        entry["literal"] = value
        read.append(entry)
    for status in (STATUS_MISSING, STATUS_DENIED, STATUS_LINKED, STATUS_UNSUPPORTED):
        bad = next((t for t in read if t["status"] == status), None)
        if bad is not None:
            return status, bad.get("reason"), read
    if derive_kind(t["kind"] for t in read) is None:
        return STATUS_UNSUPPORTED, "Its inputs take different types.", read
    return STATUS_OK, None, read


def _describe_one(prompt: dict, binding_id: str, entry: dict, class_lookup: Callable, cache: dict,
                  origin: str = "authored") -> dict:
    descriptor: dict[str, Any] = {
        "source": "binding",
        "binding_id": binding_id,
        "origin": origin,
        "label": entry["label"],
        "section": section_for(prompt, entry.get("targets"), entry.get("section", "workflow")),
        "group": entry["group"],
        "order": entry["order"],
        "advanced": entry["advanced"],
        "note": entry["note"],
        "kind": None,
        "value": None,
        "error": None,
    }
    exported = entry.get("status")
    if exported is not None and exported != STATUS_OK:
        status, reason, read = exported, None, []
    else:
        status, reason, read = _check_targets(prompt, entry.get("targets") or [], class_lookup, cache)
    descriptor["targets"] = [{"path": t["key"], "input": t["input"], "class_type": t.get("class_type"),
                              "status": t["status"]} for t in read] or [
        {"path": t["path"], "input": t["input"], "class_type": None, "status": status}
        for t in entry.get("targets") or []]
    if not descriptor["label"]:
        descriptor["label"] = _default_label(prompt, entry.get("targets") or [])
    if status == STATUS_MISSING and entry.get("restorable"):
        reason = "Reconnect it with Restore Controls on the canvas, then export again."
    descriptor["status"] = status
    descriptor["reason"] = reason or STATUS_REASONS.get(status) if status != STATUS_OK else None
    if status != STATUS_OK:
        return descriptor
    kind = derive_kind(t["kind"] for t in read)
    descriptor["kind"] = kind
    bounds, unknown, provider, static_options = _bounds_for(prompt, read)
    descriptor.update(bounds)
    if any(t.get("multiline") for t in read):
        descriptor["multiline"] = True
    for name in ("default", "label_on", "label_off"):
        if name in read[0]:
            descriptor[name] = read[0][name]
    if provider:
        descriptor["provider"] = True
        if static_options is not None:
            descriptor["static_options"] = static_options
    if unknown:
        # Some target is checked only by its node at run time; the bounds and
        # options shown are the ones every other target declares.
        descriptor["checked_at_run"] = True
    literal = read[0].get("literal")
    try:
        if kind == "BOOLEAN" and not isinstance(literal, bool):
            raise ControlValueError(f"{literal!r} is not true or false.")
        value = coerce_control_value("INT" if kind == "SEED" else kind, literal)
        check_value_for_target(value, descriptor)
        descriptor["value"] = value
    except (ControlValueError, OverflowError) as exc:
        descriptor["value"] = literal if isinstance(literal, (str, int, float, bool)) else None
        descriptor["error"] = str(exc)
    if kind == "SEED":
        descriptor["seed_mode"] = entry.get("seed_mode")
    return descriptor


def _flag_shared_targets(bindings: list[dict]) -> None:
    """Two bindings may not set one input: whichever wrote last would win, and
    two seeds would advance it twice. The export is untrusted, so check here."""
    owners: dict[tuple, list[dict]] = {}
    for descriptor in bindings:
        if descriptor["status"] != STATUS_OK:
            continue
        for target in descriptor["targets"]:
            owners.setdefault((target["path"], target["input"]), []).append(descriptor)
    for shared in owners.values():
        if len(shared) > 1:
            for descriptor in shared:
                descriptor.update(status=STATUS_DUPLICATE, kind=None, value=None,
                                  reason="Another Control sets the same input. Remove one of them.")


def describe_bindings(
    prompt: Any,
    *,
    relay_census: Any = None,
    class_lookup: Callable[[str], Any] | None = None,
    local: Any = None,
    local_notes: Any = None,
    orphan_manifest: Any = None,
) -> dict:
    """Describe the Relay's bindings and notes in an API prompt.

    ``relay_census`` is the canvas count of active, muted, bypassed and nested
    Relays, which an export cannot show. ``local`` and ``local_notes`` are the
    Sonder App's app-only bindings and notes (contract amendment A1, from the
    template's sidecar); they describe with ``origin: "local"``, also when the
    workflow has no Relay. Blocking only through ``INPUT_TYPES``; callers run it
    off the event loop like any prompt walk.
    """
    if not isinstance(prompt, dict):
        raise ValueError("A prompt must be an object of nodes.")
    class_lookup = class_lookup or _comfy_class_lookup
    cache: dict = {}
    relay = control_manifest.find_relay(prompt)
    census_error = control_manifest.census_error(relay_census)
    if census_error and relay["status"] != "error":
        relay = {**relay, "status": "error", "error": census_error}
    view: dict[str, Any] = {
        "relay": {"status": relay["status"], "key": relay["key"], "error": relay["error"]},
        "bindings": [],
        "notes": [],
        "suggestions": [],
        "editors": prompt_graph.editor_keys(prompt),
    }
    if relay["status"] == "error":
        view["error"] = relay["error"]
        return view
    manifest = relay["manifest"]
    if relay["status"] == "none" and orphan_manifest is not None:
        # A deleted Relay leaves workflow anchors. Accept only their suggestions,
        # through the same hygiene checks; they confer no exposure or value state.
        manifest = {**manifest, "suggestions": control_manifest.parse_manifest(orphan_manifest)["suggestions"]}
    for binding_id, entry in manifest["bindings"].items():
        view["bindings"].append(_describe_one(prompt, binding_id, entry, class_lookup, cache))
    for note_id, entry in manifest["notes"].items():
        view["notes"].append({"source": "note", "note_id": note_id, "origin": "authored", "text": entry["text"],
                              "group": entry["group"], "order": entry["order"], "status": STATUS_OK})
    for suggestion in manifest["suggestions"]:
        if suggestion["binding_id"] in manifest["bindings"]:
            continue
        view["suggestions"].append({**suggestion, "label": _default_label(prompt, suggestion["targets"]),
                                    "section": section_for(prompt, suggestion["targets"])})
    authored = {(t["path"], t["input"]) for d in view["bindings"] if d["status"] == STATUS_OK for t in d["targets"]}
    for binding_id, entry in clean_local_bindings(local).items():
        if binding_id in manifest["bindings"]:
            continue  # the workflow's own binding wins; the sidecar entry stays for Replace
        descriptor = _describe_one(prompt, binding_id, entry, class_lookup, cache, origin="local")
        if descriptor["status"] == STATUS_OK and any((t["path"], t["input"]) in authored for t in descriptor["targets"]):
            descriptor.update(status=STATUS_SHADOWED, kind=None, value=None, reason=STATUS_REASONS[STATUS_SHADOWED])
        view["bindings"].append(descriptor)
    for note_id, entry in clean_local_notes(local_notes).items():
        if note_id not in manifest["notes"]:
            view["notes"].append({"source": "note", "note_id": note_id, "origin": "local", **entry, "status": STATUS_OK})
    exposed = {d["binding_id"] for d in view["bindings"]}
    view["suggestions"] = [s for s in view["suggestions"] if s["binding_id"] not in exposed]
    _flag_shared_targets(view["bindings"])
    view["_presets"] = manifest["presets"]
    view["description"] = manifest["description"]
    view["groups"] = manifest["groups"]
    view["bindings"].sort(key=lambda d: (d["order"], d["label"].lower(), d["binding_id"]))
    view["notes"].sort(key=lambda d: (d["order"], d["note_id"]))
    view["group_order"] = group_order(view)
    return view


# ── App-only bindings (Sonder App, contract amendment A1) ─────────────────────

def _clean_text(value: Any, limit: int) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _clean_order(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(-PANEL_ORDER_LIMIT, min(PANEL_ORDER_LIMIT, value))


def _clean_locator(value: Any, *, with_class: bool) -> dict | None:
    if not isinstance(value, dict):
        return None
    path, name = value.get("path"), value.get("input")
    if not (isinstance(path, str) and 0 < len(path) <= 256 and isinstance(name, str) and 0 < len(name) <= 256):
        return None
    locator = {"path": path, "input": name}
    if with_class:
        class_type = value.get("class_type")
        if not isinstance(class_type, str) or not class_type:
            return None
        locator["class_type"] = class_type
    return locator


def clean_local_bindings(local: Any) -> dict:
    """The sidecar's app-only bindings as describe entries; invalid ones are skipped.

    Storage keeps every entry as written (unknown keys and invalid entries are
    preserved and ignored), so this never feeds back into the file.
    """
    entries = {}
    for binding_id, raw in (local.items() if isinstance(local, dict) else []):
        if not (isinstance(binding_id, str) and control_manifest.ID_RE.match(binding_id)
                and binding_id not in control_manifest.FORBIDDEN_KEYS and isinstance(raw, dict)):
            continue
        targets = [_clean_locator(t, with_class=True) for t in raw.get("targets") or []]
        if not targets or len(targets) > control_manifest.MAX_TARGETS or None in targets:
            continue
        section = raw.get("section")
        entries[binding_id] = {
            "label": _clean_text(raw.get("label"), PANEL_LABEL_MAX_CHARS).strip(),
            "section": section if section in control_manifest.SECTIONS else "workflow",
            "group": control_manifest.clean_group(raw.get("group")),
            "order": _clean_order(raw.get("order")),
            "advanced": raw.get("advanced") is True,
            "note": _clean_text(raw.get("note"), PANEL_NOTE_MAX_CHARS),
            "targets": targets,
            "seed_mode": None,
        }
        if len(entries) >= control_manifest.MAX_BINDINGS:
            break
    return entries


def clean_local_notes(local_notes: Any) -> dict:
    notes = {}
    for note_id, raw in (local_notes.items() if isinstance(local_notes, dict) else []):
        if not (isinstance(note_id, str) and control_manifest.ID_RE.match(note_id)
                and note_id not in control_manifest.FORBIDDEN_KEYS and isinstance(raw, dict)):
            continue
        notes[note_id] = {"text": _clean_text(raw.get("text"), PANEL_NOTE_MAX_CHARS),
                          "group": control_manifest.clean_group(raw.get("group")),
                          "order": _clean_order(raw.get("order"))}
        if len(notes) >= control_manifest.MAX_NOTES:
            break
    return notes


# ── Candidates (Manage) ──────────────────────────────────────────────────────

MAX_CANDIDATES = 500
FILTER_MAX_CHARS = 120
_INVENTORY_CACHE: dict[tuple, list] = {}
_INVENTORY_LOCK = threading.Lock()
_INVENTORY_CACHE_SIZE = 8


def clean_candidates(raw: Any, limit: int = MAX_CANDIDATES * 4) -> list[dict]:
    """Candidate locators from a request: ``{path, input, targets?, node_title?}``.

    ``path``/``input`` locate the anchor (a root node and its widget, or a
    subgraph host and its input name); ``targets`` are the API inputs it sets.
    """
    if not isinstance(raw, list) or len(raw) > limit:
        raise ControlValueError(f"Send at most {limit} candidates.")
    cleaned = []
    for item in raw:
        locator = _clean_locator(item, with_class=False)
        if locator is None or ":" in locator["path"]:
            raise ControlValueError("A candidate is invalid.")
        targets = item.get("targets")
        if targets is None:
            targets = [dict(locator)]
        if not isinstance(targets, list) or not 1 <= len(targets) <= control_manifest.MAX_TARGETS:
            raise ControlValueError("A candidate's targets are invalid.")
        targets = [_clean_locator(t, with_class=False) for t in targets]
        if None in targets:
            raise ControlValueError("A candidate's targets are invalid.")
        title = item.get("node_title")
        cleaned.append({**locator, "targets": targets,
                        "node_title": title[:PANEL_LABEL_MAX_CHARS] if isinstance(title, str) else ""})
    return cleaned


def sdk_candidates(prompt: dict) -> list[dict]:
    """Root-level literal inputs of an API workflow (amendment A1).

    Settings inside subgraphs flatten to ``host:inner`` paths and are exposed
    from ComfyUI instead; links and the legacy PrimitiveNode never qualify.
    """
    found = []
    for key, node in prompt.items():
        if ":" in str(key) or not isinstance(node, dict):
            continue
        if node.get("class_type") == binding_policy.LEGACY_PRIMITIVE_CLASS:
            continue
        for name, value in prompt_graph.node_inputs(prompt, key).items():
            if not _is_link(value) and isinstance(name, str):
                found.append({"path": str(key), "input": name, "targets": [{"path": str(key), "input": name}],
                              "node_title": ""})
    return found


def bound_targets(view: dict) -> dict:
    """(path, input) -> binding_id for every target a binding claims, whatever its status."""
    owners = {}
    for descriptor in view.get("bindings", []):
        if descriptor.get("status") == STATUS_SHADOWED:
            continue
        for target in descriptor.get("targets") or []:
            owners.setdefault((target.get("path"), target.get("input")), descriptor.get("binding_id"))
    return owners


def _inventory_key(prompt: dict, candidates: list) -> str:
    text = json.dumps([prompt, candidates], sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def candidate_inventory(prompt: dict, candidates: list[dict], *,
                        class_lookup: Callable[[str], Any] | None = None) -> list[dict]:
    """Check every candidate against the prompt: kind, status and recommendation.

    Returns one entry per candidate, in the given order, with ``status`` from the
    same target checks describe uses. Callers drop what is not ``ok`` or is
    already bound; nothing greyed out is ever listed. Cached per prompt and
    candidate list, because Manage re-reads on every graph change.
    """
    class_lookup = class_lookup or _comfy_class_lookup
    key = (class_lookup, _inventory_key(prompt, candidates))
    with _INVENTORY_LOCK:
        cached = _INVENTORY_CACHE.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
    cache: dict = {}
    inventory = []
    for candidate in candidates:
        status, reason, read = _check_targets(prompt, candidate["targets"], class_lookup, cache)
        kind = derive_kind(t.get("kind") for t in read) if status == STATUS_OK else None
        if status == STATUS_OK and kind not in CONTROL_KINDS:
            status = STATUS_UNSUPPORTED
        first = read[0] if read else {}
        node = prompt.get(candidate["path"]) if isinstance(prompt.get(candidate["path"]), dict) else None
        title = candidate.get("node_title") or (
            _node_title(node, str(node.get("class_type") or "")) if node else str(first.get("class_type") or ""))
        # A promoted input is recommended when every consumer is the same field.
        fields = {(t.get("class_type"), t.get("input")) for t in read}
        recommended = _RECOMMENDED.get(next(iter(fields))) if len(fields) == 1 else None
        targets = [{"path": t["key"], "input": t["input"], "class_type": t.get("class_type")} for t in read]
        entry = {
            "path": candidate["path"],
            "input": candidate["input"],
            "targets": targets,
            "node_title": title,
            "class_type": (node or {}).get("class_type") or first.get("class_type"),
            "label": (recommended_label(recommended, node or {"class_type": first.get("class_type")}) if recommended
                      else (f"{title} · {candidate['input']}" if title else candidate["input"])),
            "kind": kind,
            "status": status,
            "recommended": bool(recommended),
            "section": section_for(prompt, targets),
        }
        if reason:
            entry["reason"] = reason
        if recommended and recommended.hint:
            entry["hint"] = recommended.hint
        inventory.append(entry)
    with _INVENTORY_LOCK:
        _INVENTORY_CACHE[key] = inventory
        while len(_INVENTORY_CACHE) > _INVENTORY_CACHE_SIZE:
            _INVENTORY_CACHE.pop(next(iter(_INVENTORY_CACHE)))
    return copy.deepcopy(inventory)


def _matches(entry: dict, query: str) -> bool:
    haystack = " ".join(str(entry.get(name) or "") for name in ("node_title", "class_type", "input", "label"))
    return query in haystack.lower()


def manage_candidates(inventory: list[dict], view: dict, *, query: str = "",
                      limit: int = MAX_CANDIDATES) -> tuple[list[dict], bool]:
    """What Manage lists: ``ok`` and unbound candidates matching the filter,
    Recommended first. Returns (candidates, truncated)."""
    owners = bound_targets(view)
    query = (query or "").strip().lower()[:FILTER_MAX_CHARS]
    listed = [entry for entry in inventory
              if entry["status"] == STATUS_OK
              and not any((t["path"], t["input"]) in owners for t in entry["targets"])
              and (not query or _matches(entry, query))]
    listed.sort(key=lambda entry: not entry["recommended"])  # stable: node order otherwise
    return listed[:limit], len(listed) > limit


# ── Changes ──────────────────────────────────────────────────────────────────

MAX_CONTROL_CHANGES = 64
VALUE_FIELDS = ("value", "seed_mode")
PRESENTATION_FIELDS = ("label", "note", "order", "group", "advanced")


def _target_table(view: dict) -> dict:
    table: dict[str, tuple[dict, str]] = {}
    for descriptor in view.get("bindings", []):
        identity = descriptor.get("binding_id")
        if not isinstance(identity, str) or not identity:
            continue
        base = f"binding:{identity}"
        for field in PRESENTATION_FIELDS:
            table[f"{base}:{field}"] = (descriptor, field)
        if descriptor.get("status") != STATUS_OK or descriptor.get("kind") not in CONTROL_KINDS:
            continue
        table[f"{base}:value"] = (descriptor, "value")
        if descriptor.get("kind") == "SEED":
            table[f"{base}:seed_mode"] = (descriptor, "seed_mode")
    for note in view.get("notes", []):
        identity = note.get("note_id")
        if isinstance(identity, str) and identity and note.get("status") == STATUS_OK:
            for field in ("text", "order", "group"):
                table[f"note:{identity}:{field}"] = (note, field)
    return table


def coerce_binding_value(descriptor: dict, raw: Any) -> Any:
    """A binding's value in its kind; BOOLEAN takes only JSON true/false."""
    kind = descriptor.get("kind")
    if kind == "BOOLEAN":
        if not isinstance(raw, bool):
            raise ControlValueError("Choose on or off.")
        return raw
    return coerce_control_value("INT" if kind == "SEED" else kind, raw)


def _shares_node(descriptor: dict, others: list[dict]) -> bool:
    paths = {t.get("path") for t in descriptor.get("targets", [])}
    return any(paths & {t.get("path") for t in other.get("targets", [])} for other in others)


def validate_binding_changes(view: dict, changes: Any, *, prompt: dict | None = None,
                             max_changes: int | None = MAX_CONTROL_CHANGES,
                             structural: str | None = None,
                             inventory: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """Whitelist and coerce panel writes against the current descriptor view.

    Returns canonical values and target/reason refusals. Callers reject the whole
    batch on any refusal. With ``prompt``, provider options (a Selector's
    ``choice``) come from the same batch's other changes, so a new choice list
    and a choice in it are accepted together. Without it (the mounted relay's
    pre-check), such a batch defers that check to the canvas writer, which
    validates against the fresh full view. ``max_changes=None`` lifts the 64-change
    request cap for the SDK's own stored values, which may hold up to 256.

    ``structural`` admits Manage's structural targets for one host: ``"canvas"``
    (the writer edits anchors and the manifest), ``"sdk"`` (app-only bindings,
    amendment A1) or ``"relay"`` (the mounted pre-check: shape only, because the
    canvas writer re-validates against the live graph). ``inventory`` is the
    ``candidate_inventory`` of the candidates the batch names.
    """
    if not isinstance(changes, dict) or not changes or (max_changes is not None and len(changes) > max_changes):
        return {}, [{"target": "", "reason": "Send 1 to 64 changes."}]
    if "workflow:restore" in changes:
        from .control_cleanup import validate_restore
        try:
            if len(changes) != 1:
                raise ControlValueError("Restore must be the only change in its request.")
            plan = validate_restore(view, changes["workflow:restore"], mode=structural)
            if structural != "relay":
                batch = _StructuralBatch(view, structural, inventory or [])
                available = {r["binding_id"]: r for r in view["restore"]["restorable"]}
                plan = {"bindings": [{"binding_id": bid, **batch._candidate(available[bid], bid)} for bid in plan["bindings"]]}
            return {"workflow:restore": plan}, []
        except ControlValueError as exc:
            return {}, [{"target": "workflow:restore", "reason": str(exc)}]
    if "workflow:cleanup" in changes:
        from .control_cleanup import validate
        try:
            if len(changes) != 1:
                raise ControlValueError("Clean up must be the only change in its request.")
            return {"workflow:cleanup": validate(view, changes["workflow:cleanup"], mode=structural)}, []
        except ControlValueError as exc:
            return {}, [{"target": "workflow:cleanup", "reason": str(exc)}]
    # Presets dispatch by source before fields shared with bindings/notes.
    preset_targets = [target for target in changes if (parse_target(target) or (None,))[0] == "preset"]
    if preset_targets:
        from .control_presets import validate_preset_changes
        return validate_preset_changes(view, changes, prompt=prompt, structural=structural, inventory=inventory)
    table = _target_table(view)
    batch = _StructuralBatch(view, structural, inventory)
    value_changes = [table[t][0] for t in changes if t in table and table[t][1] == "value"]
    overrides: dict[tuple[str, str], Any] = {}
    for target, raw in changes.items():
        if target in table and table[target][1] == "value":
            for item in table[target][0].get("targets", []):
                overrides[(item.get("path"), item.get("input"))] = raw

    cleaned, rejected = {}, []
    for target, raw in changes.items():
        try:
            if target == "workflow:description":
                if view.get("error") or (view.get("relay") or {}).get("status") == "error":
                    raise ControlValueError("The Sonder Control Relay cannot be read.")
                if not isinstance(raw, str) or len(raw) > control_manifest.DESCRIPTION_MAX:
                    raise ControlValueError("A workflow description must be text of at most 1000 characters.")
                cleaned[target] = raw
                continue
            if is_structural(target):
                source, identity, op = parse_target(target)
                cleaned[target] = (batch.group(identity, op, raw) if source == "group"
                                   else batch.validate(source, identity, op, raw))
                continue
            if target not in table:
                raise ControlValueError("This target is unavailable. Refresh the Controls panel.")
            descriptor, field = table[target]
            if field in ("note", "text"):
                if descriptor.get("note_truncated"):
                    raise ControlValueError("Edit this note on the canvas.")
                if not isinstance(raw, str) or len(raw) > PANEL_NOTE_MAX_CHARS:
                    raise ControlValueError("A note must be text of at most 4000 characters.")
                value = raw
            elif field == "label":
                if not isinstance(raw, str) or len(raw.strip()) > PANEL_LABEL_MAX_CHARS:
                    raise ControlValueError("A label must be text of at most 120 characters.")
                value = raw.strip()
            elif field == "advanced":
                # Its own branch: a flag, never coerced through the value's kind.
                if not isinstance(raw, bool):
                    raise ControlValueError("Advanced is true or false.")
                value = raw
            elif field in ("order", "group"):
                value = coerce_panel_layout(field, raw)
                if field == "group" and value:
                    batch.claim_group(value)
            elif field == "seed_mode":
                if not isinstance(raw, str) or raw not in SEED_MODES:
                    raise ControlValueError("Choose fixed, increment, decrement or randomize.")
                value = raw
            else:
                value = coerce_binding_value(descriptor, raw)
                bounded = dict(descriptor)
                kind = "INT" if descriptor.get("kind") == "SEED" else descriptor.get("kind")
                for name in ("min", "max"):
                    if isinstance(bounded.get(name), str):
                        bounded[name] = coerce_control_value(kind, bounded[name])
                if descriptor.get("provider"):
                    others = [d for d in value_changes if d is not descriptor]
                    if prompt is not None and descriptor.get("targets"):
                        options = _batch_options(prompt, descriptor, overrides)
                        bounded.pop("options", None)
                        if options is not None:
                            bounded["options"] = options
                    elif _shares_node(descriptor, others):
                        bounded.pop("options", None)
                # A capped relay view cannot prove membership beyond its
                # preview; its canvas writer validates against the full view.
                if bounded.get("options_truncated"):
                    bounded.pop("options", None)
                check_value_for_target(value, bounded)
            cleaned[target] = value
        except (ControlValueError, OverflowError) as exc:
            rejected.append({"target": target, "reason": str(exc)})
    if prompt is not None and not rejected:
        rejected.extend(_orphaned_dependents(prompt, table, changes, overrides))
        if rejected:
            cleaned = {}
    return cleaned, rejected


_NOOP = {"noop": True}


class _StructuralBatch:
    """Validation of Manage's structural targets within one batch.

    Every op is idempotent: re-sending a landed expose, rebind, create, remove,
    hide or group op is a no-op success (``{"noop": true}``), so a retry after a
    lost acknowledgement changes nothing. A candidate already bound is refused.

    A group op validates to one *unexpanded* plan (``{"rename": new}``,
    ``{"ungroup": true}``, ``{"move": true, "before": name|None}``,
    ``{"create": true}``) that counts as one target: each writer expands it
    against its own current state in one write (the canvas inside
    ``editManifest``, the Sonder App inside one sidecar mutation), so renaming a
    group of 200 Controls never meets the 64-change cap.
    """

    def __init__(self, view: dict, mode: str | None, inventory: list[dict] | None):
        self.view, self.mode = view, mode
        self.bindings = {d.get("binding_id"): d for d in view.get("bindings", [])}
        self.notes = {d.get("note_id"): d for d in view.get("notes", [])}
        self.suggestions = {s.get("binding_id"): s for s in view.get("suggestions", [])}
        self.inventory = None if inventory is None else {(c["path"], c["input"]): c for c in inventory}
        self.owners = bound_targets(view)
        self.claimed: set = set()
        self.binding_count, self.note_count = len(self.bindings), len(self.notes)
        orders = [d.get("order") for d in [*view.get("bindings", []), *view.get("notes", [])]
                  if not d.get("group") and isinstance(d.get("order"), int) and not isinstance(d.get("order"), bool)]
        self.next_order = max(orders, default=0)
        listed = view.get("group_order")
        self.group_list = [g for g in listed if isinstance(g, str)] if isinstance(listed, list) else group_order(view)
        self.known_groups = set(self.group_list)
        self.touched_groups: set = set()

    def claim_group(self, name: str) -> None:
        """Count a group a change names; refuse past MAX_GROUPS."""
        if name not in self.known_groups:
            if len(self.known_groups) >= control_manifest.MAX_GROUPS:
                raise ControlValueError(f"At most {control_manifest.MAX_GROUPS} groups fit in one workflow.")
            self.known_groups.add(name)
            self.group_list.append(name)

    def _require_relay(self) -> None:
        if self.mode not in ("canvas", "sdk", "relay"):
            raise ControlValueError("This target is unavailable. Refresh the Controls panel.")
        relay = self.view.get("relay") or {}
        if relay.get("status") == "error":
            raise ControlValueError(f"Fix the Sonder Control Relay first: {relay.get('error') or 'it cannot be read.'}")

    def group(self, name: str, op: str, raw: Any) -> dict:
        self._require_relay()
        if name != name.strip() or control_manifest.group_name_error(name):
            raise ControlValueError(control_manifest.group_name_error(name) or "This group name is invalid.")
        if name in self.touched_groups:
            raise ControlValueError("Two changes edit the same group. Send them one at a time.")
        self.touched_groups.add(name)
        # The mounted pre-check checks shape only: the canvas writer validates
        # against its fresh full view, so a stale relay view cannot refuse.
        shape_only = self.mode == "relay"
        if op == "create":
            if not isinstance(raw, dict):
                raise ControlValueError("Send {} to create a group.")
            if name in self.known_groups and not shape_only:
                return dict(_NOOP)
            self.claim_group(name)
            return {"create": True}
        if op == "rename":
            if not isinstance(raw, str):
                raise ControlValueError("A group name must be text.")
            new = raw.strip()
            if error := control_manifest.group_name_error(new):
                raise ControlValueError(error)
            if new != name and new in self.touched_groups:
                raise ControlValueError("Two changes edit the same group. Send them one at a time.")
            self.touched_groups.add(new)
            if new == name:
                return dict(_NOOP)
            if shape_only:
                return {"rename": new}
            if name not in self.known_groups:
                if new in self.known_groups:
                    return dict(_NOOP)  # a retry of a rename that landed
                raise ControlValueError("This group is no longer in the Controls. Refresh the Controls panel.")
            if new in self.known_groups:
                raise ControlValueError(f"A group named “{new}” already exists. Choose another name.")
            self.known_groups.discard(name)
            self.known_groups.add(new)
            self.group_list = [new if g == name else g for g in self.group_list]
            return {"rename": new}
        if op == "ungroup":
            if raw is not True:
                raise ControlValueError("Send true to ungroup.")
            if shape_only:
                return {"ungroup": True}
            if name not in self.known_groups:
                return dict(_NOOP)
            self.known_groups.discard(name)
            self.group_list = [g for g in self.group_list if g != name]
            return {"ungroup": True}
        if op == "move":
            before = raw.get("before") if isinstance(raw, dict) else None
            if not isinstance(raw, dict) or (before is not None and (
                    not isinstance(before, str) or before != before.strip()
                    or control_manifest.group_name_error(before))):
                raise ControlValueError("Choose where to move the group.")
            if before == name:
                raise ControlValueError("A group can't move before itself.")
            if shape_only:
                return {"move": True, "before": before}
            if name not in self.group_list:
                raise ControlValueError("This group is no longer in the Controls. Refresh the Controls panel.")
            if before is not None and before not in self.group_list:
                raise ControlValueError("The group order changed. Refresh the Controls panel.")
            following = self.group_list[self.group_list.index(name) + 1:]
            if following[:1] == ([before] if before is not None else []):
                return dict(_NOOP)
            rest = [g for g in self.group_list if g != name]
            index = rest.index(before) if before is not None else len(rest)
            self.group_list = rest[:index] + [name] + rest[index:]
            return {"move": True, "before": before}
        raise ControlValueError("This target is unavailable. Refresh the Controls panel.")

    def _order(self) -> int:
        self.next_order = min(self.next_order + 1, PANEL_ORDER_LIMIT)
        return self.next_order

    def _candidate(self, raw: Any, binding_id: str) -> dict:
        locator = _clean_locator(raw, with_class=False)
        if locator is None or ":" in locator["path"]:
            raise ControlValueError("Choose a setting to expose.")
        if self.inventory is None:  # mounted pre-check: the canvas writer checks the graph
            return {"candidate": locator}
        entry = self.inventory.get((locator["path"], locator["input"]))
        if entry is None:
            raise ControlValueError("This setting is no longer in the workflow. Refresh the Controls panel.")
        if entry["status"] != STATUS_OK:
            raise ControlValueError(entry.get("reason") or STATUS_REASONS.get(entry["status"]) or "This setting can't be a Control.")
        keys = {(t["path"], t["input"]) for t in entry["targets"]}
        owner = next((self.owners[k] for k in keys if k in self.owners), None)
        if owner is not None and owner != binding_id:
            raise ControlValueError("This setting is already a Control.")
        if keys & self.claimed:
            raise ControlValueError("Two changes expose the same setting.")
        self.claimed |= keys
        # A Recommended label is curated; any other default is derived at
        # describe time, so an un-renamed Control stores no label.
        return {"candidate": locator, "targets": entry["targets"], "kind": entry["kind"],
                "default_label": entry["label"] if entry.get("recommended") else ""}

    @staticmethod
    def _same_targets(descriptor: dict, chosen: dict) -> bool:
        if "targets" not in chosen:
            return False
        have = {(t.get("path"), t.get("input")) for t in descriptor.get("targets") or []}
        return bool(have) and have == {(t["path"], t["input"]) for t in chosen["targets"]}

    def validate(self, source: str, identity: str, op: str, raw: Any) -> dict:
        if self.mode in ("canvas", "sdk", "relay") and identity in control_manifest.FORBIDDEN_KEYS:
            raise ControlValueError("This Control id is invalid.")
        self._require_relay()
        if source == "note":
            return self._note(identity, op, raw)
        existing = self.bindings.get(identity)
        sdk = self.mode == "sdk"
        if op in ("hide", "unhide"):
            return self._hide(existing, op, raw)
        if op == "expose":
            if not isinstance(raw, dict):
                raise ControlValueError("Choose a setting to expose.")
            # A client-sent section is ignored: section_for derives it.
            label = raw.get("label")
            if label is not None and (not isinstance(label, str) or len(label.strip()) > PANEL_LABEL_MAX_CHARS):
                raise ControlValueError("A label must be text of at most 120 characters.")
            if raw.get("candidate") is None:
                # Restore an unlisted anchor (a suggestion), keeping its id.
                if existing is not None:
                    return dict(_NOOP)
                suggestion = self.suggestions.get(identity)
                if suggestion is None and self.mode != "relay":  # a mounted view may drop suggestions to fit
                    raise ControlValueError("This setting is no longer in the workflow. Refresh the Controls panel.")
                chosen = {"candidate": None, "targets": (suggestion or {}).get("targets") or []}
            else:
                chosen = self._candidate(raw["candidate"], identity)
                if existing is not None and self.mode == "relay":
                    return {**chosen, "label": (label or "").strip()}
                if existing is not None:
                    if self._same_targets(existing, chosen):
                        return dict(_NOOP)
                    raise ControlValueError("This Control id is already in use.")
            if self.binding_count >= control_manifest.MAX_BINDINGS:
                raise ControlValueError(f"At most {control_manifest.MAX_BINDINGS} Controls fit in one workflow.")
            self.binding_count += 1
            text = (label or "").strip() or chosen.pop("default_label", "")
            chosen.pop("default_label", None)
            return {**chosen, "label": text, "order": self._order()}
        if op == "remove":
            if raw is not True:
                raise ControlValueError("Send true to remove.")
            if existing is None:
                return dict(_NOOP)
            if sdk and existing.get("origin") != "local":
                raise ControlValueError("The workflow's own Controls can be hidden here, not removed.")
            return {"remove": True}
        if op == "rebind":
            if not isinstance(raw, dict):
                raise ControlValueError("Choose a setting to rebind to.")
            if existing is None:
                raise ControlValueError("This Control is no longer in the workflow.")
            if sdk and existing.get("origin") != "local":
                raise ControlValueError("Rebind the workflow's own Controls in ComfyUI.")
            chosen = self._candidate(raw.get("candidate"), identity)
            if self.mode == "relay":
                # Only the canvas knows the current anchor locator and can
                # distinguish a landed retry from a rebind of an active item.
                return chosen
            if self._same_targets(existing, chosen):
                return dict(_NOOP)
            if existing.get("status") == STATUS_OK:
                raise ControlValueError("Only a setting that is off can be rebound.")
            chosen.pop("default_label", None)
            return chosen
        raise ControlValueError("This target is unavailable. Refresh the Controls panel.")

    def _hide(self, existing: dict | None, op: str, raw: Any, *, base: str | None = None) -> dict:
        if self.mode != "sdk":
            raise ControlValueError("Hiding is for the Sonder App. Remove the Control instead.")
        if raw is not True:
            raise ControlValueError(f"Send true to {op}.")
        if existing is None:
            raise ControlValueError("This Control is no longer in the workflow.")
        if existing.get("origin") == "local":
            raise ControlValueError("Remove an app-only item instead of hiding it.")
        return dict(_NOOP) if bool(existing.get("hidden")) == (op == "hide") else {op: True}

    def _note(self, identity: str, op: str, raw: Any) -> dict:
        existing = self.notes.get(identity)
        sdk = self.mode == "sdk"
        if op in ("hide", "unhide"):
            return self._hide(existing, op, raw)
        if op == "create":
            if not isinstance(raw, dict):
                raise ControlValueError("A note needs text.")
            text, group = raw.get("text", ""), raw.get("group", "")
            if not isinstance(text, str) or len(text) > PANEL_NOTE_MAX_CHARS:
                raise ControlValueError("A note must be text of at most 4000 characters.")
            group = coerce_panel_layout("group", group)
            if existing is not None:
                if existing.get("text") == text:
                    return dict(_NOOP)
                raise ControlValueError("This note id is already in use.")
            if self.note_count >= control_manifest.MAX_NOTES:
                raise ControlValueError(f"At most {control_manifest.MAX_NOTES} notes fit in one workflow.")
            if group:
                self.claim_group(group)
            self.note_count += 1
            order = raw.get("order")
            order = coerce_panel_layout("order", order) if order is not None else self._order()
            return {"text": text, "group": group, "order": order}
        if op == "remove":
            if raw is not True:
                raise ControlValueError("Send true to remove.")
            if existing is None:
                return dict(_NOOP)
            if sdk and existing.get("origin") != "local":
                raise ControlValueError("The workflow's own notes can be hidden here, not removed.")
            return {"remove": True}
        raise ControlValueError("This target is unavailable. Refresh the Controls panel.")


def _batch_options(prompt: dict, descriptor: dict, overrides: dict) -> list | None:
    """A provider-backed binding's options with this batch applied, intersected
    across its targets; None when any target is known only at run time."""
    lists = [descriptor["static_options"]] if isinstance(descriptor.get("static_options"), list) else []
    for target in descriptor.get("targets", []):
        fn = binding_policy.provider_for(target.get("class_type"), target.get("input"))
        if fn is None:
            continue
        options = fn(_effective_inputs(prompt, target["path"], overrides))
        if options is None:
            return None
        lists.append(options)
    if not lists:
        return None
    return [option for option in lists[0] if all(option in other for other in lists[1:])]


def _orphaned_dependents(prompt: dict, table: dict, changes: dict, overrides: dict) -> list[dict]:
    """Refuse a source change (a Selector's list) that drops the node's current
    choice: on the canvas the Selector would silently reset it, and a Sonder App
    run would fail at /prompt. Choosing from the new list in the same batch is fine."""
    refusals = []
    for target in changes:
        entry = table.get(target)
        if not entry or entry[1] != "value":
            continue
        for row in entry[0].get("targets", []):
            dependent = binding_policy.dependent_of(row.get("class_type"), row.get("input"))
            if dependent is None:
                continue
            path = row["path"]
            fn = binding_policy.provider_for(row.get("class_type"), dependent)
            effective = _effective_inputs(prompt, path, overrides)
            current = effective.get(dependent)
            options = fn(effective) if fn else None
            if options is None or _is_link(current) or not isinstance(current, str):
                continue
            if current not in options:
                refusals.append({"target": target, "reason": (
                    f"The current choice {current!r} is not in the new list. "
                    "Choose one from it in the same change.")})
    return refusals


def binding_signature(descriptor: dict) -> dict | None:
    """What a stored SDK value was saved for. A different target resets it."""
    targets = descriptor.get("targets") or []
    if not targets or descriptor.get("kind") not in CONTROL_KINDS:
        return None
    return {"class_type": targets[0].get("class_type"), "input": targets[0].get("input"),
            "kind": descriptor.get("kind")}


def json_safe(view: dict) -> dict:
    """Copy a descriptor view at the HTTP edge, preserving integer precision.

    INT and SEED values and bounds, and coerced ``binding:<id>:value`` changes,
    travel as decimal strings: a seed above 2^53 must never become a JS number.
    Non-finite numbers become null and option lists are capped for display;
    internal consumers keep exact numbers and complete lists for validation.
    """
    def copy(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            result = {}
            for name, item in value.items():
                integer_field = value.get("kind") in ("INT", "SEED") and name in (
                    "value", "min", "max", "step", "default")
                integer_field = integer_field or (isinstance(name, str) and name.startswith("binding:")
                                                  and name.endswith(":value"))
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

    return copy({key: value for key, value in view.items() if not key.startswith("_presets")})


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


def preset_states(view, presets, **kwargs):
    from .control_presets import preset_states as project
    return project(view, presets, **kwargs)
