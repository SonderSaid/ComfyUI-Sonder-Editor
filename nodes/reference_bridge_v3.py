"""Nodes 2.0 selector and bridge for staged Reference lanes."""

from __future__ import annotations

try:
    from comfy_api.v0_0_2 import io
except ModuleNotFoundError as exc:  # pragma: no cover - depends on ComfyUI version
    if exc.name not in {"comfy_api.v0_0_2", "comfy_api"}:
        raise
    from comfy_api.latest import io

from . import cache_identity
from .cache_policy import DESCRIPTOR, EXACT, HANDLE, IDENTIFIES, IDENTITY_INVALIDATION, PREPARES, normalise_outputs
from .reference_core import (
    MAX_REFERENCE_SLOTS,
    decode_reference_audio_recipe,
    decode_reference_image_recipe,
    decode_reference_prompts,
    reference_audio_recipe,
    reference_fingerprint,
    reference_image_recipe,
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

    # nodes/cache_policy.py
    SONDER_CACHE_POLICY = {
        "reference_set": HANDLE,
        "has_reference": EXACT,
        "reference_strength": EXACT,
    }
    # The cache pipeline may run execute before cache keys exist; it publishes nothing.
    SONDER_CACHE_PREPARE = PREPARES(publishes=())

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=SELECTOR_NODE_ID,
            display_name="Sonder Reference Selector",
            category="Sonder",
            description=(
                "Resolves selected Reference lanes for the active editor window without decoding media. "
                "Use has_reference to gate the branch that contains the required Reference Bridges."
            ),
            inputs=[
                ProjectType.Input("project", tooltip="Wire from the Sonder Editor project output."),
                io.String.Input(
                    "reference_lanes",
                    default="0",
                    tooltip=(
                        "Comma-separated zero-based Reference lanes. Missing lanes are skipped and do "
                        "not fall back to lane zero. Use the Selector panel to edit this list."
                    ),
                ),
            ],
            outputs=[
                ReferenceSetType.Output(
                    display_name="reference_set",
                    tooltip="Resolved Reference set for the selected lanes. Fan out to the required Reference Bridges.",
                ),
                io.Int.Output(
                    display_name="has_reference",
                    tooltip=(
                        "0 when no selected lane has a staged item effective for this window, 1 when any does. "
                        "Gate the branch containing the Bridge on this so it genuinely does not execute."
                    ),
                ),
                io.Float.Output(
                    display_name="reference_strength",
                    tooltip=(
                        "Authored strength of the first effective selected lane in lane-index order, "
                        "or 0.0 when none is effective."
                    ),
                ),
            ],
        )

    @classmethod
    def fingerprint_inputs(cls, project=None, reference_lanes="0", **_kwargs):
        return reference_fingerprint(project, reference_lanes)

    @classmethod
    def execute(cls, project, reference_lanes="0") -> io.NodeOutput:
        result = resolve_reference_set(project, reference_lanes)
        if result.get("conflicts"):
            from .reference_core import _conflict_message
            raise RuntimeError(_conflict_message(result))
        return io.NodeOutput(*normalise_outputs(
            cls, (result, int(result["has_reference"]), float(result.get("strength", 0.0)))))


def _numbered_outputs(output_type, prefix: str, noun: str):
    return [
        output_type.Output(
            id=f"{prefix}{index:02d}",
            display_name=f"{prefix}{index:02d}",
            tooltip=f"{noun} Reference payload {index}. Slot order follows staged member order.",
        )
        for index in range(1, MAX_REFERENCE_SLOTS + 1)
    ]


def _identity_invalidation(prefix: str) -> dict:
    return {f"{prefix}{index:02d}": IDENTITY_INVALIDATION for index in range(1, MAX_REFERENCE_SLOTS + 1)}


class SonderReferenceImageBridge(io.ComfyNode):
    # nodes/cache_policy.py and nodes/cache_identity.py: one identity, the decode
    # recipe plus every member file's bytes, covers r01..r16.
    SONDER_CACHE_POLICY = {"r??": DESCRIPTOR}
    SONDER_CACHE_INVALIDATION = _identity_invalidation("r")
    SONDER_CACHE_IDENTITY = IDENTIFIES(token_input="reference_set", recipe_inputs=("unused_slots",))

    @classmethod
    def cache_recipe(cls, reference_set, unused_slots="placeholder"):
        return reference_image_recipe(reference_set, unused_slots)

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=IMAGE_BRIDGE_NODE_ID,
            display_name="Sonder Reference Image Bridge",
            category="Sonder",
            description=(
                "Decodes selected homogeneous image Reference lanes in lane-index order. Non-slot "
                "assemblies emit one payload per lane; slot recipes emit one member per output."
            ),
            inputs=[
                ReferenceSetType.Input("reference_set", tooltip="Wire from Sonder Reference Selector."),
                io.Combo.Input(
                    "unused_slots",
                    options=["placeholder", "nothing"],
                    default="placeholder",
                    optional=True,
                    tooltip=(
                        "What a slot this recipe does not drive emits. 'placeholder' emits a black "
                        "image at scene size, which a node with a required input needs. 'nothing' "
                        "emits no value at all, which a node with an optional input skips entirely — "
                        "use it when a placeholder would otherwise be treated as real content."
                    ),
                ),
            ],
            outputs=_numbered_outputs(io.Image, "r", "Image"),
        )

    @classmethod
    def execute(cls, reference_set, unused_slots="placeholder") -> io.NodeOutput:
        recipe, snapshots = cache_identity.resolve(
            reference_set, IMAGE_BRIDGE_NODE_ID, lambda value: reference_image_recipe(value, unused_slots),
            "connect reference_set to a Sonder Reference Selector")
        with cache_identity.reading(snapshots):
            return io.NodeOutput(*decode_reference_image_recipe(recipe))


class SonderReferenceAudioBridge(io.ComfyNode):
    # nodes/cache_policy.py and nodes/cache_identity.py: one identity, the decode
    # recipe plus every member file's bytes, covers a01..a16.
    SONDER_CACHE_POLICY = {"a??": DESCRIPTOR}
    SONDER_CACHE_INVALIDATION = _identity_invalidation("a")
    SONDER_CACHE_IDENTITY = IDENTIFIES(token_input="reference_set", recipe_inputs=("unused_slots",))

    @classmethod
    def cache_recipe(cls, reference_set, unused_slots="placeholder"):
        return reference_audio_recipe(reference_set, unused_slots)

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=AUDIO_BRIDGE_NODE_ID,
            display_name="Sonder Reference Audio Bridge",
            category="Sonder",
            description=(
                "Decodes selected homogeneous audio Reference lanes in lane-index order, with one "
                "trimmed AUDIO output per staged member."
            ),
            inputs=[
                ReferenceSetType.Input("reference_set", tooltip="Wire from Sonder Reference Selector."),
                io.Combo.Input(
                    "unused_slots",
                    options=["placeholder", "nothing"],
                    default="placeholder",
                    optional=True,
                    tooltip=(
                        "What a slot this recipe does not drive emits. 'placeholder' emits a one-second "
                        "silent stereo track, which a node with a required input needs. 'nothing' emits "
                        "no value at all, which a node with an optional input skips entirely — use it "
                        "when a placeholder would otherwise be treated as real content."
                    ),
                ),
            ],
            outputs=_numbered_outputs(io.Audio, "a", "Audio"),
        )

    @classmethod
    def execute(cls, reference_set, unused_slots="placeholder") -> io.NodeOutput:
        recipe, snapshots = cache_identity.resolve(
            reference_set, AUDIO_BRIDGE_NODE_ID, lambda value: reference_audio_recipe(value, unused_slots),
            "connect reference_set to a Sonder Reference Selector")
        with cache_identity.reading(snapshots):
            return io.NodeOutput(*decode_reference_audio_recipe(recipe))


class SonderReferencePromptBridge(io.ComfyNode):
    # nodes/cache_policy.py
    SONDER_CACHE_POLICY = {
        "reference_prompt": EXACT,
        "reference_names": EXACT,
        "p??": EXACT,
    }
    # The cache pipeline may run execute before cache keys exist; it publishes nothing.
    SONDER_CACHE_PREPARE = PREPARES(publishes=())

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=PROMPT_BRIDGE_NODE_ID,
            display_name="Sonder Reference Prompt Bridge",
            category="Sonder",
            description=(
                "Exports one aggregate prompt and name list for all selected homogeneous lanes, "
                "followed by per-member prompts in reserved lane-index order."
            ),
            inputs=[ReferenceSetType.Input("reference_set", tooltip="Wire from Sonder Reference Selector.")],
            outputs=[
                io.String.Output(
                    id="reference_prompt",
                    display_name="reference_prompt",
                    tooltip=(
                        "Prompt derived from the staged members. A lane's override replaces its own part; "
                        "a lone lane's override replaces the whole prompt."
                    ),
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
        return io.NodeOutput(*normalise_outputs(cls, tuple(decode_reference_prompts(reference_set))))
