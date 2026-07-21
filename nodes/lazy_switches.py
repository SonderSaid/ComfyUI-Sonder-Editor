"""Lazy Sonder switch nodes using ComfyUI's V3 schema."""

from __future__ import annotations

import os
import time
from typing import Any

try:
    from comfy_api.v0_0_2 import io
except ImportError:  # pragma: no cover - depends on installed ComfyUI version
    from comfy_api.latest import io


def _clamp_int(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


def _validate_count(name: str, value: int, lo: int, hi: int) -> bool | str:
    if value < lo or value > hi:
        return f"Error: {name} must be between {lo} and {hi}, got {value}."
    return True


def _fingerprint_value(value: Any) -> Any:
    return "__linked_input__" if value is None else value


_DEFAULT_MAX_BRANCHES = int(os.environ.get("SONDER_LAZY_MAX_BRANCHES", "32"))
_DEFAULT_MAX_LANES = int(os.environ.get("SONDER_LAZY_MAX_LANES", "8"))

MAX_BRANCHES = _clamp_int(_DEFAULT_MAX_BRANCHES, 2, 100)
MAX_LANES = _clamp_int(_DEFAULT_MAX_LANES, 1, 32)

CATEGORY = "Sonder/Logic"
PACK_PREFIX = "SONDER"


def _lane_label(lane_idx: int) -> str:
    if 0 <= lane_idx < 26:
        return chr(ord("A") + lane_idx)
    return f"L{lane_idx + 1}"


class _LazySelectionMixin:
    @classmethod
    def _selected_index(cls, *, select: int) -> int:
        return int(select)

    @classmethod
    def _validate_index_max(cls, idx: int, max_exclusive: int) -> bool | str:
        if idx < 0:
            return "Error: Selected index must be >= 0."
        if idx >= max_exclusive:
            return f"Error: Selected index {idx} must be < {max_exclusive}."
        return True

    @classmethod
    def _selection_fingerprint(cls, *, select: int | None) -> tuple[Any, ...]:
        if select is not None:
            return ("resolved", cls._selected_index(select=int(select)))
        return ("linked", _fingerprint_value(select))


class SonderLazySwitch(io.ComfyNode, _LazySelectionMixin):
    """Universal lazy switch with autogrowing generic inputs."""

    _AUTOGROW_ID = "branches"
    _PREFIX = "item"

    @classmethod
    def define_schema(cls) -> io.Schema:
        template = io.MatchType.Template(f"{PACK_PREFIX}_lazy_switch_any")
        autogrow_template = io.Autogrow.TemplatePrefix(
            input=io.MatchType.Input(
                cls._PREFIX,
                template=template,
                optional=True,
                lazy=True,
                tooltip="Lazy branch input. Only the selected branch is evaluated.",
            ),
            prefix=cls._PREFIX,
            min=2,
            max=MAX_BRANCHES,
        )

        return io.Schema(
            node_id="SonderLazySwitch",
            display_name="Sonder Switch",
            category=CATEGORY,
            description=(
                "Routes any one data type across all branches and only "
                "evaluates the selected branch."
            ),
            inputs=[
                io.Int.Input(
                    "select",
                    default=0,
                    min=0,
                    max=MAX_BRANCHES - 1,
                    step=1,
                    display_mode=io.NumberDisplay.number,
                    tooltip="Zero-based selected branch index.",
                ),
                io.Autogrow.Input(
                    cls._AUTOGROW_ID,
                    template=autogrow_template,
                    tooltip="Autogrowing branch inputs. All branches must resolve to the same type.",
                ),
            ],
            outputs=[
                io.MatchType.Output(template=template, display_name="item"),
            ],
        )

    @classmethod
    def _branch_key(cls, idx: int) -> str:
        return f"{cls._PREFIX}{idx}"

    @classmethod
    def _flat_key(cls, inner: str) -> str:
        return f"{cls._AUTOGROW_ID}.{inner}"

    @classmethod
    def fingerprint_inputs(cls, select: int | None = None, **kwargs):
        return (
            "sonder_lazy_switch",
            *cls._selection_fingerprint(select=select),
            MAX_BRANCHES,
        )

    @classmethod
    def check_lazy_status(cls, select: int, branches: io.Autogrow.Type, **kwargs) -> list[str]:
        idx = cls._selected_index(select=select)
        idx_ok = cls._validate_index_max(idx, MAX_BRANCHES)
        if idx_ok is not True:
            return []

        branch_key = cls._branch_key(idx)
        if isinstance(branches, dict) and branch_key in branches and branches[branch_key] is None:
            return [cls._flat_key(branch_key)]
        return []

    @classmethod
    def validate_inputs(
        cls,
        select: int | None = 0,
        branches: io.Autogrow.Type | None = None,
        **kwargs,
    ) -> bool | str:
        if select is None:
            # 'select' is driven by a link (e.g. Sonder Selector); its value is
            # not resolved at prompt-validation time on newer ComfyUI, which
            # passes None. Defer index validation to check_lazy_status/execute.
            return True
        idx = cls._selected_index(select=select)
        idx_ok = cls._validate_index_max(idx, MAX_BRANCHES)
        if idx_ok is not True:
            return idx_ok

        branch_key = cls._branch_key(idx)
        if not isinstance(branches, dict) or branch_key not in branches:
            return f"Error: Selected branch '{branch_key}' is not connected."
        return True

    @classmethod
    def execute(cls, select: int, branches: io.Autogrow.Type, **kwargs) -> io.NodeOutput:
        idx = cls._selected_index(select=select)
        branch_key = cls._branch_key(idx)
        if not isinstance(branches, dict) or branch_key not in branches:
            raise ValueError(f"Selected branch '{branch_key}' is missing or not connected.")

        value = branches[branch_key]
        if value is None:
            raise ValueError(
                f"Selected branch '{branch_key}' is not available (None). "
                f"Lazy evaluation may not have requested '{cls._flat_key(branch_key)}'."
            )
        return io.NodeOutput(value)


class SonderLazyCluster(io.ComfyNode, _LazySelectionMixin):
    """Universal multi-lane lazy cluster with a shared selected branch."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        lane_templates = [
            io.MatchType.Template(f"{PACK_PREFIX}_cluster_lane_{lane_idx}")
            for lane_idx in range(MAX_LANES)
        ]

        branch_inputs: list[io.Input] = []
        for branch_idx in range(MAX_BRANCHES):
            for lane_idx, template in enumerate(lane_templates):
                branch_inputs.append(
                    io.MatchType.Input(
                        cls._input_name(branch_idx, lane_idx),
                        template=template,
                        optional=True,
                        lazy=True,
                        tooltip=(
                            f"Generic branch input for branch {branch_idx}, lane {lane_idx}. "
                            "Only the selected branch is evaluated."
                        ),
                    )
                )

        return io.Schema(
            node_id="SonderLazyCluster",
            display_name="Sonder Cluster",
            category=CATEGORY,
            description=(
                "Routes a shared selected branch across multiple lanes. "
                "Each lane can carry any type, but all branches within a lane "
                "must match that lane's type."
            ),
            inputs=[
                io.Int.Input(
                    "select",
                    default=0,
                    min=0,
                    max=MAX_BRANCHES - 1,
                    step=1,
                    display_mode=io.NumberDisplay.number,
                    tooltip="Zero-based selected branch index.",
                ),
                io.Int.Input(
                    "branches",
                    default=2,
                    min=2,
                    max=MAX_BRANCHES,
                    step=1,
                    display_mode=io.NumberDisplay.number,
                    tooltip="Number of visible branches in the cluster.",
                ),
                io.Int.Input(
                    "lanes",
                    default=2,
                    min=1,
                    max=MAX_LANES,
                    step=1,
                    display_mode=io.NumberDisplay.number,
                    tooltip="Number of visible lanes in the cluster.",
                ),
                *branch_inputs,
            ],
            outputs=[
                io.MatchType.Output(
                    template=template,
                    display_name=cls._output_name(lane_idx),
                )
                for lane_idx, template in enumerate(lane_templates)
            ],
        )

    @classmethod
    def _input_name(cls, branch_idx: int, lane_idx: int) -> str:
        return f"b{branch_idx}_l{lane_idx}"

    @classmethod
    def _output_name(cls, lane_idx: int) -> str:
        return _lane_label(lane_idx)

    @classmethod
    def _validate_cluster_state(cls, idx: int, branches: int, lanes: int) -> bool | str:
        branches_ok = _validate_count("branches", int(branches), 2, MAX_BRANCHES)
        if branches_ok is not True:
            return branches_ok
        lanes_ok = _validate_count("lanes", int(lanes), 1, MAX_LANES)
        if lanes_ok is not True:
            return lanes_ok
        idx_ok = cls._validate_index_max(idx, int(branches))
        if idx_ok is not True:
            return f"Error: Selected index {idx} is outside active branches ({branches})."
        return True

    @classmethod
    def fingerprint_inputs(cls, select: int | None = None, branches: int = 2, lanes: int = 2, **kwargs):
        return (
            "sonder_lazy_cluster",
            *cls._selection_fingerprint(select=select),
            int(branches),
            int(lanes),
            MAX_BRANCHES,
            MAX_LANES,
        )

    @classmethod
    def check_lazy_status(cls, select: int, branches: int, lanes: int, **kwargs) -> list[str]:
        idx = cls._selected_index(select=select)
        state_ok = cls._validate_cluster_state(idx, int(branches), int(lanes))
        if state_ok is not True:
            return []

        needed: list[str] = []
        for lane_idx in range(int(lanes)):
            key = cls._input_name(idx, lane_idx)
            if key in kwargs and kwargs[key] is None:
                needed.append(key)
        return needed

    @classmethod
    def validate_inputs(cls, **kwargs) -> bool | str:
        select_raw = kwargs.get("select", 0)
        branches_raw = kwargs.get("branches", 2)
        lanes_raw = kwargs.get("lanes", 2)
        if select_raw is None or branches_raw is None or lanes_raw is None:
            # One or more control inputs (select/branches/lanes) are driven by a
            # link and unresolved at prompt-validation time on newer ComfyUI,
            # which passes None. Defer validation to check_lazy_status/execute.
            return True
        select = int(select_raw)
        branches = int(branches_raw)
        lanes = int(lanes_raw)

        idx = cls._selected_index(select=select)
        state_ok = cls._validate_cluster_state(idx, branches, lanes)
        if state_ok is not True:
            return state_ok

        for lane_idx in range(lanes):
            key = cls._input_name(idx, lane_idx)
            if key not in kwargs:
                return f"Error: Selected branch {idx} is missing active lane {lane_idx} ('{key}')."
        return True

    @classmethod
    def execute(cls, select: int, branches: int, lanes: int, **kwargs) -> io.NodeOutput:
        idx = cls._selected_index(select=select)
        state_ok = cls._validate_cluster_state(idx, int(branches), int(lanes))
        if state_ok is not True:
            raise ValueError(state_ok)

        outputs: list[Any] = []
        for lane_idx in range(int(lanes)):
            key = cls._input_name(idx, lane_idx)
            if key not in kwargs:
                raise ValueError(f"Selected branch {idx} is missing active lane {lane_idx} ('{key}').")

            value = kwargs[key]
            if value is None:
                raise ValueError(f"Selected branch {idx} lane {lane_idx} ('{key}') is not available (None).")
            outputs.append(value)

        outputs.extend([None] * (MAX_LANES - int(lanes)))
        return io.NodeOutput(*outputs)


class SonderLazyDebugSleep(io.ComfyNode):
    """Sleeps for N seconds then passes the value through."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        template = io.MatchType.Template(f"{PACK_PREFIX}_debug_sleep")
        return io.Schema(
            node_id="SonderLazyDebugSleep",
            display_name="Sonder Lazy Debug Sleep",
            category=f"{CATEGORY}/tests",
            description="Sleeps for N seconds then returns the input value. Useful to test lazy skipping.",
            is_dev_only=True,
            inputs=[
                io.Float.Input(
                    "seconds",
                    default=1.0,
                    min=0.0,
                    max=60.0,
                    step=0.1,
                    tooltip="Sleep duration in seconds.",
                ),
                io.MatchType.Input(
                    "value",
                    template=template,
                    tooltip="Value to pass through after sleeping.",
                ),
            ],
            outputs=[
                io.MatchType.Output(template=template, display_name="value"),
            ],
        )

    @classmethod
    def execute(cls, seconds: float, value: Any) -> io.NodeOutput:
        print(f"[SonderLazyDebugSleep] sleeping {float(seconds):.2f}s")
        time.sleep(float(seconds))
        return io.NodeOutput(value)


LAZY_NODE_CLASS_MAPPINGS = {
    "SonderLazySwitch": SonderLazySwitch,
    "SonderLazyCluster": SonderLazyCluster,
    "SonderLazyDebugSleep": SonderLazyDebugSleep,
}

LAZY_NODE_DISPLAY_NAME_MAPPINGS = {
    "SonderLazySwitch": "Sonder Switch",
    "SonderLazyCluster": "Sonder Cluster",
    "SonderLazyDebugSleep": "Sonder Lazy Debug Sleep",
}
