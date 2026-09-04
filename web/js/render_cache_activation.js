import { renderCacheMaxBytes } from "./editor_settings.js";

export function applyRenderCacheSettingToNode(node, settings) {
    if (!node || node.type !== "SonderEditor") return false;
    const widget = node.widgets?.find?.((candidate) => candidate?.name === "render_cache_max_bytes");
    if (!widget) return false;
    const maxBytes = renderCacheMaxBytes(settings);
    if (widget.value === maxBytes) return false;
    // Browser-local retention policy is mirrored directly into the serialized
    // execution widget. It is deliberately not a mounted-session relay field.
    widget.value = maxBytes;
    return true;
}

export function applyRenderCacheSettingToNodes(nodes, settings) {
    let changed = 0;
    for (const node of nodes || []) {
        if (applyRenderCacheSettingToNode(node, settings)) changed += 1;
    }
    return changed;
}

// Only overlapping callers in the same lifecycle wave may share a sweep.
// Explicit clear, budget-change, and queue-idle events use a new generation.
export function shouldJoinSweep(inFlight, key, generation) {
    return !!inFlight && inFlight.key === key && inFlight.generation === generation;
}
