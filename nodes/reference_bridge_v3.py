"""Nodes 2.0 selector and bridge for staged Reference lanes."""

from __future__ import annotations

try:
    from comfy_api.v0_0_2 import io
except ModuleNotFoundError as exc:  # pragma: no cover - depends on ComfyUI version
    if exc.name not in {"comfy_api.v0_0_2", "comfy_api"}:
        raise
    from comfy_api.latest import io

from .reference_core import (
    MAX_REFERENCE_SLOTS,
    decode_reference_set,
    reference_fingerprint,
    resolve_reference_set,
)


SELECTOR_NODE_ID = "SonderReferenceSelector"
BRIDGE_NODE_ID = "SonderReferenceBridge"
ProjectType = io.Custom("SONDER_PROJECT")
ReferenceSetType = io.Custom("SONDER_REFERENCE_SET")


class SonderReferenceSelector(io.ComfyNode):
    """Resolve effective-in-window Reference presence without decoding media."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=SELECTOR_NODE_ID,
            display_name="Sonder Reference Selector",
            category="Sonder",
            description=(
                "Resolves one Reference lane for the active editor window without decoding media. "
                "Use has_reference to gate the branch that contains Sonder Reference Bridge."
            ),
            inputs=[
                ProjectType.Input("project", tooltip="Wire from the Sonder Editor project output."),
                io.Int.Input(
                    "reference_lane_index",
                    default=0,
                    min=0,
                    max=999,
                    step=1,
                    display_mode=io.NumberDisplay.number,
                    tooltip="Zero-based Reference lane. Missing lanes do not fall back to lane zero.",
                ),
            ],
            outputs=[
                ReferenceSetType.Output(
                    display_name="reference_set",
                    tooltip="Resolved Reference set for the selected lane. Wire to Sonder Reference Bridge.",
                ),
                io.Int.Output(
                    display_name="has_reference",
                    tooltip=(
                        "0 when no staged item is effective for this window, 1 when one is. "
                        "Gate the branch containing the Bridge on this so it genuinely does not execute."
                    ),
                ),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, project=None, reference_lane_index=0, **_kwargs):
        return reference_fingerprint(project, reference_lane_index)

    @classmethod
    def execute(cls, project, reference_lane_index=0) -> io.NodeOutput:
        result = resolve_reference_set(project, reference_lane_index)
        return io.NodeOutput(result, int(result["has_reference"]))


class SonderReferenceBridge(io.ComfyNode):
    """Decode and assemble a selected Reference set inside the active branch."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        # Tooltips stay generic: a socket cannot know which lane it is carrying,
        # so per-recipe meaning (which outputs are live, how slots map to model
        # positions) belongs in the Reference lane panel, not here.
        fixed_outputs = [
            io.Image.Output(
                display_name="reference_frames",
                tooltip="Staged members assembled per the lane recipe — a batch, a composited sheet, or a temporal sequence.",
            ),
            io.Int.Output(
                display_name="reference_idx",
                tooltip="Frame index each member occupies in the assembled sequence. Meaningful only for recipes that place references in time.",
            ),
            io.Float.Output(
                display_name="reference_strength",
                tooltip="Conditioning strength for the assembled set, 0.0 when this recipe does not drive it.",
            ),
            io.Audio.Output(
                display_name="reference_audio",
                tooltip="Trimmed audio for an audio Reference lane. Image recipes emit the required silent fallback rather than None.",
            ),
            io.String.Output(
                display_name="reference_prompt",
                tooltip="Prompt derived from the staged members, or the item's override. Bridge-local; the scene prompt lanes remain authoritative for composition.",
            ),
            io.String.Output(
                display_name="reference_names",
                tooltip="Comma-separated Library names of the staged members, in slot order.",
            ),
            io.Image.Output(
                display_name="context",
                tooltip="Background or context member routed out of the main set, for recipes with a dedicated background input.",
            ),
        ]
        slots = [
            io.Image.Output(
                display_name=f"r{index:02d}",
                tooltip=(
                    f"Staged member {index} as its own image, for models that take numbered reference "
                    "sockets. Slot order is the order staged on the lane."
                ),
            )
            for index in range(1, MAX_REFERENCE_SLOTS + 1)
        ]
        return io.Schema(
            node_id=BRIDGE_NODE_ID,
            display_name="Sonder Reference Bridge",
            category="Sonder",
            description=(
                "Decodes and assembles the selected Reference set according to its durable lane recipe. "
                "Absent references emit type-correct compatibility fallbacks; unreadable staged media raises."
            ),
            inputs=[
                ReferenceSetType.Input("reference_set", tooltip="Wire from Sonder Reference Selector."),
            ],
            outputs=[*fixed_outputs, *slots],
        )

    @classmethod
    def execute(cls, reference_set) -> io.NodeOutput:
        return io.NodeOutput(*decode_reference_set(reference_set))
