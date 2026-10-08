// Sonder Control Relay on the canvas.
//
// Hides the Relay's `manifest` widget and makes its Export (API) value carry
// each binding's targets (control_bindings.exportManifest, pure and total).
// Installs the anchor copy rule on every node, so a pasted, cloned or inserted
// copy of an exposed node never claims the original's binding. The Controls
// panel and its canvas host own every edit; this module writes nothing else.
import { app } from "/scripts/app.js";
import { commitWidgetVisibility, setWidgetHidden } from "./widget_visibility.js";
import { exportManifest, installAnchorCopyRule, isRelay } from "./control_bindings.js";

const EXT_NAME = "sonder.control_relay";
const INSTALLED = Symbol("sonderRelayInstalled");
const rootGraph = () => app.rootGraph || app.graph || null;
const isConfiguring = () => !!app.configuringGraph;

function installRelay(node) {
    if (!isRelay(node) || node[INSTALLED]) return;
    const manifest = (node.widgets || []).find(widget => widget?.name === "manifest");
    if (!manifest) return;
    node[INSTALLED] = true;
    const serialize = manifest.serializeValue;
    // Export (API), Run and the Controls describe all read this; the workflow
    // keeps the saved presentation, because graphToPrompt serializes the
    // workflow before calling serializeValue, and this never writes the value.
    manifest.serializeValue = function () {
        const saved = typeof this.value === "string" ? this.value : "";
        try {
            return exportManifest(saved, rootGraph());
        } catch {
            return serialize ? serialize.apply(this, arguments) : saved;
        }
    };
    if (setWidgetHidden(manifest, true)) commitWidgetVisibility(node, { resize: !app.configuringGraph });
}

app.registerExtension({
    name: EXT_NAME,
    nodeCreated(node) {
        // First, and synchronously: a paste configures the node right after
        // this hook, and the copy rule must already be in its chain.
        installAnchorCopyRule(node, isConfiguring);
        installRelay(node);
    },
    loadedGraphNode(node) {
        installAnchorCopyRule(node, isConfiguring);
        installRelay(node);
    },
});
