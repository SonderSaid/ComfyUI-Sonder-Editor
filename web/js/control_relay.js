// Sonder Control Relay on the canvas.
//
// Hides the Relay's `manifest` widget and makes its Export (API) value carry
// each binding's targets (control_bindings.exportManifest, pure and total).
// Installs the anchor copy rule on every node, so a pasted, cloned or inserted
// copy of an exposed node never claims the original's binding. Draws the
// Relay's rows (control_relay_rows.js): editable proxies of its bound widgets,
// refreshed every 400 ms while the Relay is on screen or after a graph change.
// The Controls panel and its canvas host own every other edit.
import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";
import { commitWidgetVisibility, setWidgetHidden } from "./widget_visibility.js";
import { exportManifest, installAnchorCopyRule, isRelay } from "./control_bindings.js";
import { syncRelayRows } from "./control_relay_rows.js";

const EXT_NAME = "sonder.control_relay";
const INSTALLED = Symbol("sonderRelayInstalled");
const REFRESH_MS = 400;
const rootGraph = () => app.rootGraph || app.graph || null;
const isConfiguring = () => !!app.configuringGraph;
const relays = new Set();
let timer = null,
    graphChanged = true;

// A row being typed in, dragged or edited in a dialog is never rebuilt.
function isEditing(relay) {
    const canvas = app.canvas;
    if (canvas?.node_widget?.[0] === relay) return true;
    const active = document.activeElement;
    if (active?.closest?.(`[data-node-id="${relay.id}"]`)
        && (active.matches?.("input, textarea, select, [role='combobox']") || active.isContentEditable)) return true;
    return !!document.querySelector?.(".graphdialog, .litegraph .dialog") && canvas?.current_node === relay;
}

function onScreen(relay) {
    const ids = app.canvas?._visible_node_ids;
    if (ids instanceof Set) return ids.has(relay.id);
    const visible = app.canvas?.visible_nodes;
    return Array.isArray(visible) ? visible.includes(relay) : true;
}

function tick() {
    const changed = graphChanged;
    graphChanged = false;
    if (document.hidden) {
        graphChanged = changed;
        return;
    }
    for (const relay of relays) {
        if (!relay.graph) {
            relays.delete(relay);
            continue;
        }
        if (!changed && !onScreen(relay)) continue;
        try {
            syncRelayRows(relay, { root: rootGraph(), canvas: app.canvas, isEditing });
        } catch (error) {
            console.warn("[Sonder] Relay rows could not refresh", error);
        }
    }
    if (!relays.size) {
        clearInterval(timer);
        timer = null;
    }
}

// Nodes 2.0 renders a widget from its own state (frontend 1.53.10 widget value
// store). setWidgetHidden hides through a per-widget options copy, which that
// state never sees for the manifest's DOM textarea, so the widget's own
// `hidden` setter is called too. Every Relay hides its manifest.
function hideFromNodes2(widget) {
    for (let proto = Object.getPrototypeOf(widget); proto; proto = Object.getPrototypeOf(proto)) {
        const setter = Object.getOwnPropertyDescriptor(proto, "hidden")?.set;
        if (setter) {
            setter.call(widget, true);
            return;
        }
    }
}

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
    hideFromNodes2(manifest);
    if (setWidgetHidden(manifest, true)) commitWidgetVisibility(node, { resize: !app.configuringGraph });
    relays.add(node);
    graphChanged = true;
    timer ||= setInterval(tick, REFRESH_MS);
}

app.registerExtension({
    name: EXT_NAME,
    setup() {
        api.addEventListener?.("graphChanged", () => { graphChanged = true; });
    },
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
