"""ComfyUI-LTX-Editor: NLE-style video editing and AI generation pipeline."""

# Guard all ComfyUI-specific imports — this file is loaded by pytest
# during test discovery, where folder_paths etc. are unavailable.
try:
    from .nodes.editor_node import LTXEditor
    from .nodes.io_nodes import LTXSaveVideo, LTXPreviewVideo

    NODE_CLASS_MAPPINGS = {
        "LTXEditor": LTXEditor,
        "LTXSaveVideo": LTXSaveVideo,
        "LTXPreviewVideo": LTXPreviewVideo,
    }

    NODE_DISPLAY_NAME_MAPPINGS = {
        "LTXEditor": "LTX Editor",
        "LTXSaveVideo": "LTX Save Video",
        "LTXPreviewVideo": "LTX Preview Video",
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
