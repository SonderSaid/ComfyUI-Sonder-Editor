import { app } from "/scripts/app.js";
import { refreshAutogrowShape, slotValueIndex } from "./autogrow_passthrough.js";

const EXT_NAME = "sonder.metadata_collector";
const TARGET = "SonderMetadataCollector";
const MAX_COLLECTOR_INPUTS = 12;

function hideWidget(widget) {
    if (!widget || widget.hidden) return;
    widget.hidden = true;
    widget._sonderCollectorOrigComputeSize = widget.computeSize;
    widget.computeSize = () => [0, -4];
}

function showWidget(widget) {
    if (!widget || !widget.hidden) return;
    widget.hidden = false;
    if (widget._sonderCollectorOrigComputeSize) {
        widget.computeSize = widget._sonderCollectorOrigComputeSize;
        delete widget._sonderCollectorOrigComputeSize;
    } else {
        delete widget.computeSize;
    }
}

function visibleValueCount(node) {
    let max = -1;
    for (const slot of node?.inputs || []) {
        const index = slotValueIndex(slot);
        if (index >= 0) max = Math.max(max, index);
    }
    return Math.max(1, max + 1);
}

function refreshCollector(node) {
    refreshAutogrowShape(node, {
        maxCount: MAX_COLLECTOR_INPUTS,
        inputs: true,
        outputs: false,
    });
    const visible = visibleValueCount(node);
    for (const widget of node.widgets || []) {
        const match = /^label_(\d+)$/.exec(widget?.name || "");
        if (!match) continue;
        const index = parseInt(match[1], 10);
        if (index < visible) showWidget(widget);
        else hideWidget(widget);
    }
    if (typeof node.computeSize === "function" && typeof node.setSize === "function") {
        node.setSize(node.computeSize());
    }
}

app.registerExtension({
    name: EXT_NAME,
    beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData.name !== TARGET) return;

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
