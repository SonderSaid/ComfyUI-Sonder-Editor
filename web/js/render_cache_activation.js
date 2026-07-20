import { isRenderCacheEnabled } from "./editor_settings.js";

export function applyRenderCacheSettingToNode(node, settings) {
    if (!node || node.type !== "SonderEditor") return false;
    const widget = node.widgets?.find?.((candidate) => candidate?.name === "render_cache_enabled");
    if (!widget) return false;
    const enabled = isRenderCacheEnabled(settings);
    if (widget.value === enabled) return false;
    // Browser-local retention policy is mirrored directly into the serialized
    // execution widget. It is deliberately not a mounted-session relay field.
    widget.value = enabled;
    return true;
}

export function applyRenderCacheSettingToNodes(nodes, settings) {
    let changed = 0;
    for (const node of nodes || []) {
        if (applyRenderCacheSettingToNode(node, settings)) changed += 1;
    }
    return changed;
}
