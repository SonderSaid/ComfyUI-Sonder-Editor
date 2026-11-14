"""The media carrier: an internal node that hands Editor media to its consumers by content.

The cache pipeline (``server/cache_pipeline.py``) inserts one carrier per Editor
media output as an **ephemeral** node of the prompt being executed, keyed by a
token derived from a digest of that output's content (``nodes/cache_content.py``),
and rewires the output's consumers to it. ComfyUI then keys each consumer by the
token instead of the Editor's NaN, so a consumer of unchanged media is reused
from its cache. The carrier never appears in a submitted prompt, a saved
workflow, hidden ``PROMPT`` or history.

``execute`` resolves the token only through the run record of the prompt that is
executing (``sdk_runs.media_for``), which points into the Editor's per-prompt
memo: the memo stays the only owner of the render, and a token typed into a
prompt resolves to nothing. For the Editor's deferred frames and audio (Phase
4b) the token is a pre-render identity, and the slot is settled before any node
runs; a carrier that finds it still deferred materializes it from its recipe. ``validate_inputs`` refuses every submitted carrier,
because ComfyUI validates only submitted nodes, never ephemeral ones.

V3, dev-only: it is listed in ``/object_info`` but not offered in the node search
outside dev mode. Registered only with ``comfy_api``; without it the pipeline
plans no carriers.
"""

from __future__ import annotations

try:
    from comfy_api.v0_0_2 import io
except ImportError as exc:  # pragma: no cover - depends on installed ComfyUI version
    if exc.name not in {"comfy_api.v0_0_2", "comfy_api.latest", "comfy_api"}:
        raise
    from comfy_api.latest import io

from ..server import sdk_runs
from . import editor_media

try:
    from comfy_execution.utils import get_executing_context
except ImportError:  # pragma: no cover - comfy_api without comfy_execution is not a supported install
    def get_executing_context():
        return None

NODE_ID = "SonderMediaCarrier"
DISPLAY_NAME = "Sonder Media Carrier (internal)"
REFUSED = ("The Sonder Media Carrier is internal to the Sonder cache pipeline and cannot be part of a "
           "submitted workflow. Connect the Sonder Editor's output directly.")


class SonderMediaCarrier(io.ComfyNode):
    """Returns the Editor media this run registered under ``token`` (cache pipeline, internal)."""

    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id=NODE_ID,
            display_name=DISPLAY_NAME,
            category="Sonder/internal",
            description="Internal to the Sonder cache pipeline: carries the Sonder Editor's media by content.",
            is_dev_only=True,
            inputs=[io.String.Input("token")],
            outputs=[io.AnyType.Output("media")],
        )

    @classmethod
    def validate_inputs(cls, token):
        return REFUSED

    @classmethod
    def execute(cls, token):
        context = get_executing_context()
        found, value = (sdk_runs.media_for(getattr(context, "prompt_id", None), token)
                        if context is not None else (False, None))
        if not found:
            raise RuntimeError("This media carrier holds a token this run did not create. "
                               "Run the workflow again.")
        # A deferred render or mix (Phase 4b) that nothing settled before this
        # carrier ran (a pipeline fault): materialize it now, once, from its recipe.
        value = editor_media.settle_carrier(getattr(context, "prompt_id", None), token, value)
        return io.NodeOutput(value)
