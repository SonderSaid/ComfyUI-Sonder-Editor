"""Sonder Template Helper: reads Model Template values from a model's VAE.

Today it reads the **Audio grid** (the audio encoder's sample rate and crop block) that a
template needs for a model without a built-in one; ``nodes/template_rules.py`` owns the
rules. The node id is deliberately general: later values (the frame rule) are appended as
new outputs, which never breaks a saved workflow.

An output node, so it runs and shows its report on the node with nothing else wired.
It never loads or runs the model, and it reads no project, so ComfyUI caches it like any
pure node.
"""

from __future__ import annotations

try:
    from comfy_api.v0_0_2 import io, ui
except ImportError as exc:  # pragma: no cover - depends on installed ComfyUI version
    # Only a missing versioned API falls back; an unrelated broken import inside
    # ComfyUI must surface instead of being masked by the fallback.
    if exc.name not in {"comfy_api.v0_0_2", "comfy_api.latest", "comfy_api"}:
        raise
    from comfy_api.latest import io, ui

from .cache_policy import CACHEABLE, INHERITS
from .template_rules import audio_grid_from_vae

NODE_ID = "SonderTemplateHelper"
DISPLAY_NAME = "Sonder Template Helper"
_PURE = "a pure function of its VAE input; ComfyUI's own cache applies"
# Older ComfyUI schemas have no search aliases; never pass a field the schema lacks.
_ALIASES = ({"search_aliases": ["audio grid", "model template", "VAE", "probe"]}
            if "search_aliases" in getattr(io.Schema, "__dataclass_fields__", {}) else {})


class SonderTemplateHelper(io.ComfyNode):
    """Reports the Model Template values a VAE implies (today: the Audio grid)."""

    # nodes/cache_policy.py: selected as an output node; nothing per-run.
    SONDER_CACHE_POLICY = {"audio_rate": INHERITS(_PURE), "audio_block": INHERITS(_PURE),
                           "report": INHERITS(_PURE)}
    SONDER_CACHE_EXECUTION = CACHEABLE(_PURE)

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id=NODE_ID,
            display_name=DISPLAY_NAME,
            category="Sonder",
            description=(
                "Reads Model Template values from a model's VAE, for setting up a custom template. "
                "Wire the model's audio VAE and run: it reports the Audio grid (sample rate and block) "
                "to enter as a Custom Audio grid, checked against the VAE's own crop. Gives 0, 0 with "
                "the reason when no grid applies. It never loads or runs the model."
            ),
            inputs=[
                io.Vae.Input("vae", tooltip="The model's audio VAE, from its VAE loader."),
            ],
            outputs=[
                io.Int.Output(display_name="audio_rate",
                              tooltip="The audio encoder's sample rate, or 0 when no grid applies."),
                io.Int.Output(display_name="audio_block",
                              tooltip="The audio encoder's crop block in samples, or 0 when no grid applies."),
                io.String.Output(display_name="report", tooltip="What was found and checked, and why."),
            ],
            is_output_node=True,
            **_ALIASES,
        )

    @classmethod
    def execute(cls, vae) -> io.NodeOutput:
        result = audio_grid_from_vae(vae)
        report = "\n".join(result["lines"])
        return io.NodeOutput(int(result["rate"]), int(result["block"]), report, ui=ui.PreviewText(report))
