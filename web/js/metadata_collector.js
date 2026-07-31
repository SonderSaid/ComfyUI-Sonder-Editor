import { app } from "/scripts/app.js";
import { refreshAutogrowShape } from "./autogrow_passthrough.js";
import {
    collectorCapacity,
    collectorLabelIndex,
    visibleCollectorValueCount,
} from "./metadata_collector_shape.js";
import {
    commitWidgetVisibility,
    setWidgetHidden,
} from "./widget_visibility.js";

const EXT_NAME = "sonder.metadata_collector";
const LEGACY_TARGET = "SonderMetadataCollector";
const V3_TARGET = "SonderMetadataCollectorV3";
const TARGETS = new Set([LEGACY_TARGET, V3_TARGET]);

const isLegacyCollector = (node) =>
    node?.comfyClass === LEGACY_TARGET || node?.type === LEGACY_TARGET;

function refreshCollector(node) {
    const capacity = collectorCapacity(node);
    if (isLegacyCollector(node)) {
        refreshAutogrowShape(node, {
            maxCount: capacity,
            inputs: true,
            outputs: false,
            resize: false,
            dirty: false,
        });
    }

    const visible = visibleCollectorValueCount(node, capacity);
    let visibilityChanged = false;
    for (const widget of node.widgets || []) {
        const index = collectorLabelIndex(widget);
        if (index < 0) continue;
        visibilityChanged = setWidgetHidden(widget, index >= visible) || visibilityChanged;
    }

    if (visibilityChanged) commitWidgetVisibility(node, { resize: false, dirty: false });
    if (typeof node.computeSize === "function" && typeof node.setSize === "function") {
        node.setSize(node.computeSize());
    }
    node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
    name: EXT_NAME,
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (!TARGETS.has(nodeData.name)) return;

        const origOnNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            origOnNodeCreated?.apply(this, arguments);
            refreshCollector(this);
        };

        const origConnectionsChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function () {
            const result = origConnectionsChange?.apply(this, arguments);
            refreshCollector(this);
            return result;
        };
    },
});
