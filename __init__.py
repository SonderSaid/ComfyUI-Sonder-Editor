"""ComfyUI-Sonder-Editor: timeline video editing and AI generation pipeline."""

import logging

# Guard all ComfyUI-specific imports — this file is loaded by pytest
# during test discovery, where folder_paths etc. are unavailable.
try:
    from .nodes.editor_node import SonderEditor
    from .nodes.io_nodes import SonderSaveBridge, SonderSaveVideo, SonderPreviewVideo
    from .nodes.bridge_nodes import SonderGuidesBridgeStart, SonderGuidesBridgeEnd
    from .nodes.driver_bridge import SonderDriverBridge, SonderDriverSelector
    from .nodes.masks_bridge import SonderMasksBridge
    from .nodes.metadata_collector import SonderMetadataCollector
    from .nodes.prompt_bridge import SonderPromptRelayBridge
    from .nodes.selector import SonderSelector

    NODE_CLASS_MAPPINGS = {
        "SonderEditor": SonderEditor,
        "SonderMetadataCollector": SonderMetadataCollector,
        "SonderSaveBridge": SonderSaveBridge,
        "SonderSaveVideo": SonderSaveVideo,
        "SonderPreviewVideo": SonderPreviewVideo,
        "SonderGuidesBridgeStart": SonderGuidesBridgeStart,
        "SonderGuidesBridgeEnd": SonderGuidesBridgeEnd,
        "SonderDriverSelector": SonderDriverSelector,
        "SonderDriverBridge": SonderDriverBridge,
        "SonderMasksBridge": SonderMasksBridge,
        "SonderPromptRelayBridge": SonderPromptRelayBridge,
        "SonderSelector": SonderSelector,
    }

    NODE_DISPLAY_NAME_MAPPINGS = {
        "SonderEditor": "Sonder Editor",
        "SonderMetadataCollector": "Sonder Metadata Collector",
        "SonderSaveBridge": "Sonder Save Bridge",
        "SonderSaveVideo": "Sonder Save Video",
        "SonderPreviewVideo": "Sonder Preview Video",
        "SonderGuidesBridgeStart": "Sonder Guides Bridge Start",
        "SonderGuidesBridgeEnd": "Sonder Guides Bridge End",
        "SonderDriverSelector": "Sonder Driver Selector",
        "SonderDriverBridge": "Sonder Driver Bridge",
        "SonderMasksBridge": "Sonder Masks Bridge",
        "SonderPromptRelayBridge": "Sonder Prompt Relay Bridge",
        "SonderSelector": "Sonder Selector",
    }

    try:
        from .nodes.lazy_switches import LAZY_NODE_CLASS_MAPPINGS, LAZY_NODE_DISPLAY_NAME_MAPPINGS
        NODE_CLASS_MAPPINGS.update(LAZY_NODE_CLASS_MAPPINGS)
        NODE_DISPLAY_NAME_MAPPINGS.update(LAZY_NODE_DISPLAY_NAME_MAPPINGS)
    except Exception as exc:
        logging.getLogger(__name__).warning("Sonder lazy nodes unavailable: %s", exc)

    try:
        from .nodes.metadata_collector_v3 import (
            V3_NODE_ID,
            SonderMetadataCollectorV3,
        )
        _v3_collector_schema = SonderMetadataCollectorV3.GET_SCHEMA()
        if getattr(_v3_collector_schema, "node_id", None) != V3_NODE_ID:
            raise RuntimeError(
                "Sonder Metadata Collector V3 schema returned an unexpected node id"
            )
        if (
            getattr(_v3_collector_schema, "display_name", None)
            != "Sonder Metadata Collector Nodes 2.0"
        ):
            raise RuntimeError(
                "Sonder Metadata Collector V3 schema returned an unexpected display name"
            )
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("comfy_api"):
            logging.getLogger(__name__).warning(
                "Sonder Metadata Collector V3 import failed: %s",
                exc,
                exc_info=True,
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Sonder Metadata Collector V3 unavailable: %s",
            exc,
            exc_info=True,
        )
    else:
        NODE_CLASS_MAPPINGS.update({
            V3_NODE_ID: SonderMetadataCollectorV3,
        })
        NODE_DISPLAY_NAME_MAPPINGS.update({
            V3_NODE_ID: "Sonder Metadata Collector Nodes 2.0",
            "SonderMetadataCollector": "Sonder Metadata Collector",
        })

    try:
        from .nodes.reference_bridge_v3 import (
            AUDIO_BRIDGE_NODE_ID,
            IMAGE_BRIDGE_NODE_ID,
            PROMPT_BRIDGE_NODE_ID,
            SELECTOR_NODE_ID,
            SonderReferenceAudioBridge,
            SonderReferenceImageBridge,
            SonderReferencePromptBridge,
            SonderReferenceSelector,
        )
        _reference_selector_schema = SonderReferenceSelector.GET_SCHEMA()
        _reference_image_bridge_schema = SonderReferenceImageBridge.GET_SCHEMA()
        _reference_audio_bridge_schema = SonderReferenceAudioBridge.GET_SCHEMA()
        _reference_prompt_bridge_schema = SonderReferencePromptBridge.GET_SCHEMA()
        if getattr(_reference_selector_schema, "node_id", None) != SELECTOR_NODE_ID:
            raise RuntimeError("Sonder Reference Selector schema returned an unexpected node id")
        if getattr(_reference_image_bridge_schema, "node_id", None) != IMAGE_BRIDGE_NODE_ID:
            raise RuntimeError("Sonder Reference Image Bridge schema returned an unexpected node id")
        if getattr(_reference_audio_bridge_schema, "node_id", None) != AUDIO_BRIDGE_NODE_ID:
            raise RuntimeError("Sonder Reference Audio Bridge schema returned an unexpected node id")
        if getattr(_reference_prompt_bridge_schema, "node_id", None) != PROMPT_BRIDGE_NODE_ID:
            raise RuntimeError("Sonder Reference Prompt Bridge schema returned an unexpected node id")
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("comfy_api"):
            logging.getLogger(__name__).warning(
                "Sonder Reference V3 import failed: %s", exc, exc_info=True
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Sonder Reference V3 nodes unavailable: %s", exc, exc_info=True
        )
    else:
        NODE_CLASS_MAPPINGS.update({
            SELECTOR_NODE_ID: SonderReferenceSelector,
            IMAGE_BRIDGE_NODE_ID: SonderReferenceImageBridge,
            AUDIO_BRIDGE_NODE_ID: SonderReferenceAudioBridge,
            PROMPT_BRIDGE_NODE_ID: SonderReferencePromptBridge,
        })
        NODE_DISPLAY_NAME_MAPPINGS.update({
            SELECTOR_NODE_ID: "Sonder Reference Selector",
            IMAGE_BRIDGE_NODE_ID: "Sonder Reference Image Bridge",
            AUDIO_BRIDGE_NODE_ID: "Sonder Reference Audio Bridge",
            PROMPT_BRIDGE_NODE_ID: "Sonder Reference Prompt Bridge",
        })

    try:
        from .nodes.control_relay import DISPLAY_NAME as RELAY_DISPLAY_NAME, NODE_ID as RELAY_NODE_ID, SonderControlRelay
        _relay_schema = SonderControlRelay.GET_SCHEMA()
        if getattr(_relay_schema, "node_id", None) != RELAY_NODE_ID:
            raise RuntimeError("Sonder Control Relay schema returned an unexpected node id")
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("comfy_api"):
            logging.getLogger(__name__).warning(
                "Sonder Control Relay import failed: %s", exc, exc_info=True
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Sonder Control Relay unavailable: %s", exc, exc_info=True
        )
    else:
        NODE_CLASS_MAPPINGS[RELAY_NODE_ID] = SonderControlRelay
        NODE_DISPLAY_NAME_MAPPINGS[RELAY_NODE_ID] = RELAY_DISPLAY_NAME

    try:
        from .nodes.template_helper import DISPLAY_NAME as HELPER_DISPLAY_NAME, NODE_ID as HELPER_NODE_ID, SonderTemplateHelper
        _helper_schema = SonderTemplateHelper.GET_SCHEMA()
        if getattr(_helper_schema, "node_id", None) != HELPER_NODE_ID:
            raise RuntimeError("Sonder Template Helper schema returned an unexpected node id")
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("comfy_api"):
            logging.getLogger(__name__).warning(
                "Sonder Template Helper import failed: %s", exc, exc_info=True
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Sonder Template Helper unavailable: %s", exc, exc_info=True
        )
    else:
        NODE_CLASS_MAPPINGS[HELPER_NODE_ID] = SonderTemplateHelper
        NODE_DISPLAY_NAME_MAPPINGS[HELPER_NODE_ID] = HELPER_DISPLAY_NAME

    # Internal to the cache pipeline (never in a saved workflow). Without it the
    # pipeline plans no carriers and Editor media consumers rerun as before.
    try:
        from .nodes.media_carrier import DISPLAY_NAME as CARRIER_DISPLAY_NAME, NODE_ID as CARRIER_NODE_ID, SonderMediaCarrier
        _carrier_schema = SonderMediaCarrier.GET_SCHEMA()
        if getattr(_carrier_schema, "node_id", None) != CARRIER_NODE_ID:
            raise RuntimeError("Sonder Media Carrier schema returned an unexpected node id")
    except ModuleNotFoundError as exc:
        if not (exc.name or "").startswith("comfy_api"):
            logging.getLogger(__name__).warning(
                "Sonder Media Carrier import failed: %s", exc, exc_info=True
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Sonder Media Carrier unavailable: %s", exc, exc_info=True
        )
    else:
        NODE_CLASS_MAPPINGS[CARRIER_NODE_ID] = SonderMediaCarrier
        NODE_DISPLAY_NAME_MAPPINGS[CARRIER_NODE_ID] = CARRIER_DISPLAY_NAME

    WEB_DIRECTORY = "./web"

    # Import server module to register API routes with PromptServer
    try:
        from .server import routes  # noqa: F401
    except Exception:
        pass

    __all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]

except ImportError:
    # Running outside ComfyUI (e.g., pytest) — node registration not available
    pass
