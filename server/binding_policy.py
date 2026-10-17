"""Which inputs a Controls binding may set, and where dynamic options come from.

Pure and standard-library only. ``web/js/control_bindings.js`` mirrors the
denylist to hide candidates early; ``tests/test_control_manifest.py`` keeps the
two equal. The server stays the authority: describe, SDK apply and the mounted
relay all refuse a denied target.
"""

from __future__ import annotations

from typing import Any, Callable

from .editor_inputs import editor_optional_inputs
from .control_manifest import RELAY_CLASS_TYPE

DENIED_REASON = "Sonder sets this input itself, so it can't be a Control."

# Inputs the SDK runner writes on every Generate (``sdk_runner._build_base_locked``).
_RUNNER_OWNED = {
    "SonderEditor": frozenset({"project", *editor_optional_inputs()}),
}

# Inputs a Sonder frontend hides and drives. The SDK cannot see frontend hiding,
# so the server denies them by class. The Reference lanes, Driver lane and Save
# Bridge folder are hidden too, but they are plain user choices: they are
# Controls with pickers (``PICKERS``).
_FRONTEND_OWNED = {
    "SonderGuidesBridgeStart": frozenset({"iteration_index", "bridge_overrides_json"}),
    "SonderDriverSelector": frozenset({"driver_selector_overrides_json"}),
    RELAY_CLASS_TYPE: frozenset({"manifest"}),
}

# (class_type, input) -> the picker the Controls panel shows instead of a plain
# field. A presentation hint only: the kind, signature, presets and stored values
# are the input's own. ``web/js/control_bindings.js`` mirrors it (parity-tested).
PICKERS: dict[tuple[str, str], str] = {
    ("SonderReferenceSelector", "reference_lanes"): "reference_lanes",
    ("SonderDriverSelector", "driver_lane_index"): "driver_lane",
    ("SonderSaveBridge", "target_folder"): "asset_folder",
}


def picker_for(class_type: Any, input_name: Any) -> str | None:
    return PICKERS.get((class_type, input_name))

# Provenance label widgets of the Metadata Collectors (``label_1``...).
_DENIED_PREFIXES = {
    "SonderMetadataCollector": "label_",
    "SonderMetadataCollectorV3": "label_",
}


def denied_inputs() -> dict[str, list[str]]:
    """The exact-name denylist, for the frontend parity test."""
    merged: dict[str, set[str]] = {}
    for table in (_RUNNER_OWNED, _FRONTEND_OWNED):
        for class_type, names in table.items():
            merged.setdefault(class_type, set()).update(names)
    return {class_type: sorted(names) for class_type, names in sorted(merged.items())}


def denied_prefixes() -> dict[str, str]:
    return dict(_DENIED_PREFIXES)


def is_denied(class_type: Any, input_name: Any) -> bool:
    # Hidden inputs (PROMPT, UNIQUE_ID...) never reach an API prompt, and a
    # hidden declaration describes as unsupported; only a real widget named
    # `prompt` (common on text encoders) can be bound, so no name rule here.
    if not isinstance(input_name, str):
        return True
    for table in (_RUNNER_OWNED, _FRONTEND_OWNED):
        if input_name in table.get(class_type, ()):
            return True
    prefix = _DENIED_PREFIXES.get(class_type)
    return bool(prefix and input_name.startswith(prefix))


# ── Options providers ────────────────────────────────────────────────────────

def parse_choice_list(choice_list: Any) -> list[str]:
    """``nodes/selector.py`` ``_parse_choice_list``, kept here so the server does
    not import a node module; ``tests/test_control_manifest.py`` pins parity."""
    return [line.strip() for line in str(choice_list or "").splitlines() if line.strip()]


def _selector_options(inputs: dict) -> list[str] | None:
    raw = inputs.get("choice_list")
    if isinstance(raw, list):
        return None  # a linked list is only known at run time
    return parse_choice_list(raw)


# (class_type, input) -> options from the node's *effective* literal inputs
# (the prompt overlaid with the same batch's changes). None means "checked at run".
OPTION_PROVIDERS: dict[tuple[str, str], Callable[[dict], list | None]] = {
    ("SonderSelector", "choice"): _selector_options,
}


# (class_type, source input) -> the provider-backed input whose options it sets.
DEPENDENTS: dict[tuple[str, str], str] = {
    ("SonderSelector", "choice_list"): "choice",
}


# The legacy frontend PrimitiveNode is virtual: it never reaches an API prompt,
# and its widget mirrors the input it feeds. Binding it waits for a later
# phase (Feature Suggestions); the core ``Primitive*`` nodes are ordinary.
LEGACY_PRIMITIVE_CLASS = "PrimitiveNode"


def provider_for(class_type: Any, input_name: Any):
    return OPTION_PROVIDERS.get((class_type, input_name))


def dependent_of(class_type: Any, input_name: Any) -> str | None:
    return DEPENDENTS.get((class_type, input_name))
