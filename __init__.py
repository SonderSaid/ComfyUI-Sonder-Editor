"""ComfyUI-Sonder-Editor: NLE-style video editing and AI generation pipeline."""

# Guard all ComfyUI-specific imports — this file is loaded by pytest
# during test discovery, where folder_paths etc. are unavailable.
try:
    from .nodes.editor_node import SonderEditor
    from .nodes.io_nodes import SonderSaveBridge, SonderSaveVideo, SonderPreviewVideo
    from .nodes.bridge_nodes import SonderGuidesBridgeStart, SonderGuidesBridgeEnd

    NODE_CLASS_MAPPINGS = {
        "SonderEditor": SonderEditor,
        "SonderSaveBridge": SonderSaveBridge,
        "SonderSaveVideo": SonderSaveVideo,
        "SonderPreviewVideo": SonderPreviewVideo,
        "SonderGuidesBridgeStart": SonderGuidesBridgeStart,
        "SonderGuidesBridgeEnd": SonderGuidesBridgeEnd,
    }

    NODE_DISPLAY_NAME_MAPPINGS = {
        "SonderEditor": "Sonder Editor",
        "SonderSaveBridge": "Sonder Save Bridge",
        "SonderSaveVideo": "Sonder Save Video",
        "SonderPreviewVideo": "Sonder Preview Video",
        "SonderGuidesBridgeStart": "Sonder Guides Bridge Start",
        "SonderGuidesBridgeEnd": "Sonder Guides Bridge End",
    }

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
