"""V3 Metadata Collector schema and legacy-workflow replacement mapping."""

from __future__ import annotations

from typing import Any

try:
    from comfy_api.v0_0_2 import ComfyAPI, ComfyExtension, io
except ModuleNotFoundError as exc:  # pragma: no cover - depends on ComfyUI version
    if exc.name not in {"comfy_api.v0_0_2", "comfy_api"}:
        raise
    from comfy_api.latest import ComfyAPI, ComfyExtension, io

from .metadata_collector import (
    MAX_V3_COLLECTOR_INPUTS,
    collect_metadata,
    collector_fingerprint,
)


LEGACY_NODE_ID = "SonderMetadataCollector"
V3_NODE_ID = "SonderMetadataCollectorV3"
ProjectType = io.Custom("SONDER_PROJECT")
# ComfyUI frontend v1.45.21 only surfaces replacements for missing node types
# and explicitly skips dot-notation link targets during replacement. The
# legacy collector must remain registered, and V3 Autogrow inputs require
# values.value_N targets, so publishing this descriptor would not currently
# provide a safe user-confirmed migration. Keep the complete descriptor ready
# and tested, but do not expose it until upstream preserves those links.
METADATA_COLLECTOR_REPLACEMENT_ENABLED = False


def _label_values(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in kwargs.items()
        if isinstance(key, str) and key.startswith("label_")
    }


def _hidden_value(cls, name: str):
    hidden = getattr(cls, "hidden", None)
    return getattr(hidden, name, None)


class SonderMetadataCollectorV3(io.ComfyNode):
    """Native V3/autogrow adapter over the shared metadata collector core."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        autogrow_template = io.Autogrow.TemplatePrefix(
            input=io.AnyType.Input(
                "value",
                optional=True,
                tooltip="Any upstream value whose source widget metadata should be collected.",
            ),
            prefix="value_",
            min=1,
            max=MAX_V3_COLLECTOR_INPUTS,
        )
        return io.Schema(
            node_id=V3_NODE_ID,
            display_name="Sonder Metadata Collector Nodes 2.0",
            category="Sonder/IO",
            description="Collects explicitly wired upstream widget values into Sonder asset metadata.",
            inputs=[
                ProjectType.Input("project"),
                io.Autogrow.Input(
                    "values",
                    template=autogrow_template,
                    optional=True,
                    tooltip="Autogrowing heterogeneous metadata sources.",
                ),
                *[
                    io.String.Input(
                        f"label_{index}",
                        default="",
                        multiline=False,
                        optional=True,
                    )
                    for index in range(MAX_V3_COLLECTOR_INPUTS)
                ],
            ],
            outputs=[
                ProjectType.Output(display_name="project"),
            ],
            hidden=[
                io.Hidden.prompt,
                io.Hidden.extra_pnginfo,
                io.Hidden.unique_id,
            ],
        )

    @classmethod
    def validate_inputs(cls, **_kwargs) -> bool:
        return True

    @classmethod
    def fingerprint_inputs(
        cls,
        project=None,
        values: io.Autogrow.Type | None = None,
        **kwargs,
    ):
        del project, values
        return collector_fingerprint(
            _hidden_value(cls, "prompt"),
            _hidden_value(cls, "extra_pnginfo"),
            _label_values(kwargs),
        )

    @classmethod
    def execute(
        cls,
        project,
        values: io.Autogrow.Type | None = None,
        **kwargs,
    ) -> io.NodeOutput:
        result = collect_metadata(
            project,
            prompt=_hidden_value(cls, "prompt"),
            extra_pnginfo=_hidden_value(cls, "extra_pnginfo"),
            unique_id=_hidden_value(cls, "unique_id"),
            values=values,
            labels=_label_values(kwargs),
            capacity=MAX_V3_COLLECTOR_INPUTS,
        )
        return io.NodeOutput(result)


def build_metadata_collector_replacement() -> io.NodeReplace:
    input_mapping = [
        {"new_id": "project", "old_id": "project"},
    ]
    input_mapping.extend(
        {
            "new_id": f"values.value_{index}",
            "old_id": f"value_{index}",
        }
        for index in range(MAX_V3_COLLECTOR_INPUTS)
    )
    input_mapping.extend(
        {
            "new_id": f"label_{index}",
            "old_id": f"label_{index}",
        }
        for index in range(MAX_V3_COLLECTOR_INPUTS)
    )
    return io.NodeReplace(
        new_node_id=V3_NODE_ID,
        old_node_id=LEGACY_NODE_ID,
        old_widget_ids=[
            f"label_{index}" for index in range(MAX_V3_COLLECTOR_INPUTS)
        ],
        input_mapping=input_mapping,
        output_mapping=[
            {"new_idx": 0, "old_idx": 0},
        ],
    )


class SonderMetadataCollectorExtension(ComfyExtension):
    async def on_load(self) -> None:
        if not METADATA_COLLECTOR_REPLACEMENT_ENABLED:
            return
        api = ComfyAPI()
        await api.node_replacement.register(build_metadata_collector_replacement())

    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [SonderMetadataCollectorV3]
