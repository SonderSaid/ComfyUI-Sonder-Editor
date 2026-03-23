"""ComfyUI-LTX-Editor: NLE-style video editing and AI generation pipeline."""

# Guard all ComfyUI-specific imports — this file is loaded by pytest
# during test discovery, where folder_paths etc. are unavailable.
try:
    from .nodes.editor_node import LTXEditor
    from .nodes.io_nodes import LTXSaveVideo, LTXPreviewVideo

    # Keep old nodes temporarily for backward compat during transition
    from .nodes.project_node import LTXProjectLoader, LTXProjectInfo, LTXProjectSave
    from .nodes.io_nodes import LTXLoadVideo, LTXLoadAudio

    NODE_CLASS_MAPPINGS = {
        # --- New editor node ---
        "LTXEditor": LTXEditor,
        # --- I/O nodes (keeping) ---
        "LTXSaveVideo": LTXSaveVideo,
        "LTXPreviewVideo": LTXPreviewVideo,
        # --- Legacy nodes (will be removed once editor is fully working) ---
        "LTXProjectLoader": LTXProjectLoader,
        "LTXProjectInfo": LTXProjectInfo,
        "LTXProjectSave": LTXProjectSave,
        "LTXLoadVideo": LTXLoadVideo,
        "LTXLoadAudio": LTXLoadAudio,
    }

    NODE_DISPLAY_NAME_MAPPINGS = {
        "LTXEditor": "LTX Editor",
        "LTXSaveVideo": "LTX Save Video",
        "LTXPreviewVideo": "LTX Preview Video",
        "LTXProjectLoader": "LTX Project Loader (Legacy)",
        "LTXProjectInfo": "LTX Project Info (Legacy)",
        "LTXProjectSave": "LTX Project Save (Legacy)",
        "LTXLoadVideo": "LTX Load Video (Legacy)",
        "LTXLoadAudio": "LTX Load Audio (Legacy)",
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
