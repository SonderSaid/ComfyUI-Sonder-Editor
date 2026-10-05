"""Workflow-embedded Markdown for the Controls panel, retained in API exports."""

from __future__ import annotations

try:
    from comfy_api.v0_0_2 import io
except ImportError as exc:  # pragma: no cover - installed ComfyUI version
    if exc.name not in {"comfy_api.v0_0_2", "comfy_api"}:
        raise
    from comfy_api.latest import io

from ..server.control_descriptors import PANEL_NOTE_CLASS_TYPE
from .sonder_control import PANEL_ORDER_LIMIT

NODE_ID = PANEL_NOTE_CLASS_TYPE
DISPLAY_NAME = "Sonder Panel Note"


class SonderPanelNote(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=NODE_ID,
            display_name=DISPLAY_NAME,
            category="Sonder/Controls",
            description=(
                "Markdown text in the Sonder Controls panel: a title, introduction or instructions. "
                "Panel order and group place it among the Controls. It has no connections and "
                "does not run, but travels with an Export (API) workflow."
            ),
            inputs=[
                io.String.Input("text", default="", multiline=True, socketless=True, optional=True,
                                tooltip="Markdown text shown in the Controls panel."),
                io.Int.Input("panel_order", display_name="Panel order", default=0,
                             min=-PANEL_ORDER_LIMIT, max=PANEL_ORDER_LIMIT,
                             socketless=True, optional=True,
                             tooltip="Lower numbers are listed first in the Controls panel."),
                io.String.Input("panel_group", display_name="Panel group", default="",
                                socketless=True, optional=True,
                                tooltip='Empty is the main section; "Advanced" starts collapsed.'),
                io.String.Input("note_id", default="", socketless=True, optional=True,
                                tooltip="Stable identity, set automatically."),
            ],
            outputs=[],
        )

    @classmethod
    def execute(cls, text: str = "", panel_order: int = 0,
                panel_group: str = "", note_id: str = "") -> io.NodeOutput:
        return io.NodeOutput()


NODE_CLASS_MAPPINGS = {NODE_ID: SonderPanelNote}
NODE_DISPLAY_NAME_MAPPINGS = {NODE_ID: DISPLAY_NAME}
