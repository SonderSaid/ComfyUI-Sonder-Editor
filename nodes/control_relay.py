"""Sonder Control Relay: the workflow's curated Controls menu.

The Relay carries one hidden ``manifest`` string and nothing else. The settings
it exposes stay on their own nodes: each one is marked by an anchor in that
node's properties (``web/js/control_relay.js``), and the Controls panel writes
the native widget itself. Saved in a workflow, the manifest holds presentation
only (labels, sections, groups, order, notes); Export (API) adds each binding's
target inputs, so the Sonder App and the panel read the same menu.

The Relay has no outputs and is not an output node, so ComfyUI validates and
runs only output nodes and their ancestors, and never runs it. Parsing and
checks live in ``server/control_manifest.py`` and ``server/control_descriptors.py``.
"""

from __future__ import annotations

try:
    from comfy_api.v0_0_2 import io
except ImportError as exc:  # pragma: no cover - depends on installed ComfyUI version
    if exc.name not in {"comfy_api.v0_0_2", "comfy_api"}:
        raise
    from comfy_api.latest import io

from ..server.control_manifest import RELAY_CLASS_TYPE

NODE_ID = RELAY_CLASS_TYPE
DISPLAY_NAME = "Sonder Control Relay"


class SonderControlRelay(io.ComfyNode):
    """The Controls menu of a workflow: which settings the Controls panel shows."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=NODE_ID,
            display_name=DISPLAY_NAME,
            category="Sonder/Controls",
            description=(
                "The workflow's Controls menu. Settings exposed through it appear in the Sonder "
                "Controls panel in every editor mode and in the Sonder App, with labels, groups "
                "and Markdown notes. The settings stay on their own nodes; the Relay never runs. "
                "Keep one per workflow, in the main graph."
            ),
            inputs=[
                io.String.Input(
                    "manifest",
                    default="",
                    multiline=True,
                    socketless=True,
                    optional=True,
                    tooltip="The Controls menu, maintained by Sonder. Edit it through the Controls panel.",
                ),
            ],
            outputs=[],
        )

    @classmethod
    def execute(cls, manifest: str = "") -> io.NodeOutput:
        # Never scheduled (no outputs, not an output node); kept total anyway.
        return io.NodeOutput()


NODE_CLASS_MAPPINGS = {NODE_ID: SonderControlRelay}
NODE_DISPLAY_NAME_MAPPINGS = {NODE_ID: DISPLAY_NAME}
