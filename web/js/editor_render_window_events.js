// Browser-local signal for render-window state that is not a project mutation.
// Reference bridge shape depends on these editor widgets, so project-version
// subscriptions alone cannot keep the canvas sockets current.

export const EDITOR_RENDER_WINDOW_CHANGED = "sonder-editor-render-window-changed";

const WINDOW_FIELDS = new Set([
    "scene_id",
    "selection_start",
    "selection_end",
    "pre_context_frames",
    "post_context_frames",
]);

export function isEditorRenderWindowField(name) {
    return WINDOW_FIELDS.has(String(name || ""));
}

export function emitEditorRenderWindowChanged(detail = {}) {
    if (typeof window === "undefined" || typeof window.dispatchEvent !== "function") return;
    window.dispatchEvent(new CustomEvent(EDITOR_RENDER_WINDOW_CHANGED, { detail }));
}

export function onEditorRenderWindowChanged(callback) {
    if (typeof window === "undefined" || typeof window.addEventListener !== "function") {
        return () => {};
    }
    const listener = (event) => callback?.(event?.detail || {});
    window.addEventListener(EDITOR_RENDER_WINDOW_CHANGED, listener);
    return () => window.removeEventListener(EDITOR_RENDER_WINDOW_CHANGED, listener);
}
