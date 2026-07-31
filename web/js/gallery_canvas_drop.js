export function resolveAssetLoader(assetType, registeredNodeTypes = {}) {
    if (assetType === "image") {
        return { nodeType: "LoadImage", widgetName: "image" };
    }
    if (assetType === "video") {
        if (registeredNodeTypes["VHS_LoadVideo"]) {
            return { nodeType: "VHS_LoadVideo", widgetName: "video" };
        }
        if (registeredNodeTypes["LoadVideo"]) {
            return { nodeType: "LoadVideo", widgetName: "file" };
        }
        return null;
    }
    if (assetType === "audio" && registeredNodeTypes["LoadAudio"]) {
        return { nodeType: "LoadAudio", widgetName: "audio" };
    }
    return null;
}

export function addComboValue(widget, value) {
    if (!widget || typeof value !== "string" || !value) return false;
    if (!widget.options) widget.options = {};
    if (!widget.options.values) widget.options.values = [];
    if (!Array.isArray(widget.options.values)) return false;
    if (widget.options.values.includes(value)) return false;
    widget.options.values.push(value);
    return true;
}

export function assignUploadedAsset(node, widgetName, value) {
    const widget = node?.widgets?.find((candidate) => candidate.name === widgetName);
    if (!widget) return false;

    addComboValue(widget, value);
    const oldValue = widget.value;
    widget.value = value;
    widget.callback?.(value);
    node.onWidgetChanged?.(widget.name, value, oldValue, widget);
    return true;
}
