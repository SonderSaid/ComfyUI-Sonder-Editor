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
    decode_reference_audios,
    decode_reference_images,
    decode_reference_prompts,
    reference_fingerprint,
    resolve_reference_set,
)


SELECTOR_NODE_ID = "SonderReferenceSelector"
IMAGE_BRIDGE_NODE_ID = "SonderReferenceImageBridge"
AUDIO_BRIDGE_NODE_ID = "SonderReferenceAudioBridge"
PROMPT_BRIDGE_NODE_ID = "SonderReferencePromptBridge"
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
                "Use has_reference to gate the branch that contains the required Reference Bridges."
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
                    tooltip="Resolved Reference set for the selected lane. Fan out to the required Reference Bridges.",
                ),
                io.Int.Output(
                    display_name="has_reference",
                    tooltip=(
                        "0 when no staged item is effective for this window, 1 when one is. "
                        "Gate the branch containing the Bridge on this so it genuinely does not execute."
                    ),
                ),
                io.Float.Output(
                    display_name="reference_strength",
                    tooltip="Authored strength of the effective Reference item, or 0.0 when none is effective.",
                ),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, project=None, reference_lane_index=0, **_kwargs):
        return reference_fingerprint(project, reference_lane_index)

    @classmethod
    def execute(cls, project, reference_lane_index=0) -> io.NodeOutput:
        result = resolve_reference_set(project, reference_lane_index)
        return io.NodeOutput(result, int(result["has_reference"]), float(result.get("strength", 0.0)))


def _numbered_outputs(output_type, prefix: str, noun: str):
    return [
        output_type.Output(
            id=f"{prefix}{index:02d}",
            display_name=f"{prefix}{index:02d}",
            tooltip=f"{noun} Reference payload {index}. Slot order follows staged member order.",
        )
        for index in range(1, MAX_REFERENCE_SLOTS + 1)
    ]


class SonderReferenceImageBridge(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=IMAGE_BRIDGE_NODE_ID,
            display_name="Sonder Reference Image Bridge",
            category="Sonder",
            description=(
                "Decodes an image Reference lane. Non-slot assemblies emit their one assembled batch "
                "or sequence on r01; slot recipes emit one member per output."
            ),
            inputs=[ReferenceSetType.Input("reference_set", tooltip="Wire from Sonder Reference Selector.")],
            outputs=_numbered_outputs(io.Image, "r", "Image"),
        )

    @classmethod
    def execute(cls, reference_set) -> io.NodeOutput:
        return io.NodeOutput(*decode_reference_images(reference_set))


class SonderReferenceAudioBridge(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=AUDIO_BRIDGE_NODE_ID,
            display_name="Sonder Reference Audio Bridge",
            category="Sonder",
            description="Decodes an audio Reference lane into one trimmed AUDIO output per staged member.",
            inputs=[ReferenceSetType.Input("reference_set", tooltip="Wire from Sonder Reference Selector.")],
            outputs=_numbered_outputs(io.Audio, "a", "Audio"),
        )

    @classmethod
    def execute(cls, reference_set) -> io.NodeOutput:
        return io.NodeOutput(*decode_reference_audios(reference_set))


class SonderReferencePromptBridge(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=PROMPT_BRIDGE_NODE_ID,
            display_name="Sonder Reference Prompt Bridge",
            category="Sonder",
            description="Exports the aggregate Reference prompt and names, followed by per-member prompts.",
            inputs=[ReferenceSetType.Input("reference_set", tooltip="Wire from Sonder Reference Selector.")],
            outputs=[
                io.String.Output(
                    id="reference_prompt",
                    display_name="reference_prompt",
                    tooltip="Prompt derived from the staged members, or the item's override.",
                ),
                io.String.Output(
                    id="reference_names",
                    display_name="reference_names",
                    tooltip="Comma-separated Library names in staged order.",
                ),
                *_numbered_outputs(io.String, "p", "Prompt"),
            ],
        )

    @classmethod
    def execute(cls, reference_set) -> io.NodeOutput:
        return io.NodeOutput(*decode_reference_prompts(reference_set))
