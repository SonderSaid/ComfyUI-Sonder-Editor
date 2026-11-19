// Sonder Template Helper — shows the node's report on the node, as ComfyUI's own
// Preview Any does: the frontend's text preview widget (not saved with the
// workflow), updated when the node runs and when stored outputs are restored.
// Older frontends without that widget get a read-only multiline text box.

import { app } from "/scripts/app.js";
import { graphsOf } from "./control_bindings.js";

const NODE_TYPE = "SonderTemplateHelper";
const FALLBACK_NAME = "report_preview";

function previewApi() {
    const api = window.comfyAPI?.textPreviewWidgets;
    return typeof api?.addTextPreviewWidgets === "function" && typeof api?.updateTextPreviewWidgets === "function"
        ? api : null;
}

function addReportWidget(node) {
    const api = previewApi();
    if (api) {
        api.addTextPreviewWidgets(node);
        return;
    }
    // Read lazily: a frontend without it still loads this file; the report stays on the `report` output.
    const widgets = window.comfyAPI?.widgets?.ComfyWidgets;
    if (typeof widgets?.STRING !== "function") return;
    const widget = widgets.STRING(node, FALLBACK_NAME, ["STRING", { multiline: true }], app).widget;
    if (widget.inputEl) widget.inputEl.readOnly = true;
    widget.serialize = false;
    widget.options = { ...(widget.options || {}), serialize: false };
}

function showReport(node, output) {
    const api = previewApi();
    if (api) {
        api.updateTextPreviewWidgets(node, output);
        return;
    }
    const widget = node.widgets?.find((w) => w.name === FALLBACK_NAME);
    if (!widget) return;
    const text = output?.text;
    widget.value = Array.isArray(text) ? text.filter((t) => t != null).join("\n\n") : String(text ?? "");
}

// A stored output's key is the node's locator: "<id>" at the root, "<subgraph id>:<id>" inside one.
function nodeForLocator(locator) {
    const root = app.rootGraph || app.graph;
    if (!root) return null;
    const text = String(locator);
    const split = text.lastIndexOf(":");
    const id = split < 0 ? text : text.slice(split + 1);
    const graphId = split < 0 ? null : text.slice(0, split);
    const graph = graphId == null ? root : graphsOf(root).find((g) => String(g?.id) === graphId);
    return graph?.getNodeById?.(Number(id)) ?? graph?.getNodeById?.(id) ?? null;
}

app.registerExtension({
    name: "Sonder.TemplateHelper",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        if (nodeData?.name !== NODE_TYPE) return;
        const onNodeCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            onNodeCreated?.apply(this, arguments);
            addReportWidget(this);
            // Room for the report without scrolling; a saved size still wins on load.
            const [width, height] = this.size || [0, 0];
            this.setSize?.([Math.max(width, 460), Math.max(height, 300)]);
        };
        const onExecuted = nodeType.prototype.onExecuted;
        nodeType.prototype.onExecuted = function (output) {
            onExecuted?.apply(this, arguments);
            showReport(this, output);
        };
    },
    onNodeOutputsUpdated(outputs) {
        for (const [locator, output] of Object.entries(outputs || {})) {
            const node = nodeForLocator(locator);
            if ((node?.comfyClass || node?.type) === NODE_TYPE) showReport(node, output);
        }
    },
});
