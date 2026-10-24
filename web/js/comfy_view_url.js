// ComfyUI's `/view` URL for a file descriptor {filename, subfolder, type}. The
// one builder for graph-card players and Output View panes. `apiURL` is the
// host's own (the Sonder App has no ComfyUI page); without it, the ComfyUI
// page's API, then the bare path.

export function viewUrl(descriptor, apiURL = null) {
    if (!descriptor || !descriptor.filename) return "";
    const params = new URLSearchParams({
        filename: String(descriptor.filename),
        subfolder: String(descriptor.subfolder || ""),
        type: String(descriptor.type || "output"),
    });
    const path = `/view?${params.toString()}`;
    if (typeof apiURL === "function") return apiURL(path);
    const api = globalThis.window?.comfyAPI?.api?.api || null;
    return api?.apiURL ? api.apiURL(path) : path;
}
