"""Sonder Control: one curated value that drives the widget inputs it feeds.

The Control has no linked inputs and one ``value`` output of any type. Its
``kind`` widget is hidden and set by the frontend from the inputs it feeds, so
rewiring changes the node's cache signature and a cached value of the old type
is never served. ``execute`` re-checks the kind against the prompt and applies
the target's range and options itself, because ComfyUI skips both for a linked
input. Conversion and checks live in ``server/control_descriptors.py``.
"""

from __future__ import annotations

try:
    from comfy_api.v0_0_2 import io
except ImportError as exc:  # pragma: no cover - depends on installed ComfyUI version
    if exc.name not in {"comfy_api.v0_0_2", "comfy_api"}:
        raise
    from comfy_api.latest import io

from ..server.control_descriptors import CONTROL_CLASS_TYPE, CONTROL_KINDS, resolve_control_value


NODE_ID = CONTROL_CLASS_TYPE
DISPLAY_NAME = "Sonder Control"
SEED_MAX = 0xFFFFFFFFFFFFFFFF
PANEL_ORDER_LIMIT = 9999


def _hidden_value(cls, name: str):
    hidden = getattr(cls, "hidden", None)
    return getattr(hidden, name, None)


class SonderControl(io.ComfyNode):
    """A value for the Controls panel, wired to the inputs it should drive."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=NODE_ID,
            display_name=DISPLAY_NAME,
            category="Sonder/Controls",
            description=(
                "One value you can change from the Sonder Controls panel. Connect its output to "
                "the inputs it should drive; its type follows what it is connected to. The node's "
                "title is the control's label."
            ),
            inputs=[
                io.String.Input(
                    "value_text",
                    display_name="Value",
                    default="",
                    socketless=True,
                    tooltip="The value sent to every connected input, converted to their type.",
                ),
                io.Int.Input(
                    "seed",
                    default=0,
                    min=0,
                    max=SEED_MAX,
                    # Fixed until a seed input is connected: a seed that moves on
                    # every Run would make everything this Control feeds re-run.
                    control_after_generate=io.ControlAfterGenerate.fixed,
                    socketless=True,
                    tooltip="The seed sent when this Control drives a seed input.",
                ),
                io.Combo.Input(
                    "kind",
                    display_name="Type",
                    options=list(CONTROL_KINDS),
                    default="STRING",
                    socketless=True,
                    tooltip=(
                        "Set automatically from the connected inputs. Shown only when they accept "
                        "any type; then choose the type the value should have."
                    ),
                ),
                io.Int.Input(
                    "panel_order",
                    display_name="Panel order",
                    default=0,
                    min=-PANEL_ORDER_LIMIT,
                    max=PANEL_ORDER_LIMIT,
                    socketless=True,
                    tooltip="Lower numbers are listed first in the Controls panel.",
                ),
                io.String.Input(
                    "panel_group",
                    display_name="Panel group",
                    default="",
                    socketless=True,
                    tooltip='The Controls panel section. Empty is the main section; "Advanced" starts collapsed.',
                ),
                io.String.Input(
                    "control_id",
                    default="",
                    socketless=True,
                    tooltip="Stable identity, set automatically.",
                ),
            ],
            outputs=[
                io.AnyType.Output(
                    "value",
                    display_name="value",
                    tooltip="Connect to the inputs this Control should drive.",
                ),
            ],
            hidden=[io.Hidden.prompt, io.Hidden.unique_id],
        )

    @classmethod
    def execute(
        cls,
        value_text: str = "",
        seed: int = 0,
        kind: str = "STRING",
        panel_order: int = 0,
        panel_group: str = "",
        control_id: str = "",
    ) -> io.NodeOutput:
        del panel_order, panel_group, control_id
        value = resolve_control_value(
            _hidden_value(cls, "prompt"),
            _hidden_value(cls, "unique_id"),
            kind=kind,
            value_text=value_text,
            seed=seed,
        )
        return io.NodeOutput(value)


NODE_CLASS_MAPPINGS = {NODE_ID: SonderControl}
NODE_DISPLAY_NAME_MAPPINGS = {NODE_ID: DISPLAY_NAME}
