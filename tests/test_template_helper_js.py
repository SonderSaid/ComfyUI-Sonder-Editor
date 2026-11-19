"""The Sonder Template Helper's frontend: the report on the node, in both frontend shapes.

Runs `web/js/template_helper.js` in Node with its two imports replaced by stubs: a fake
`app` that records the registered extension, and `graphsOf` over a fake root graph.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = r"""
import { readFileSync } from "node:fs";
const source = readFileSync(__SOURCE__, "utf8")
    .split(/\r?\n/).filter((line) => !line.startsWith("import ")).join("\n");

function run(withPreviewApi) {
    const calls = [];
    let extension = null;
    const app = { registerExtension(ext) { extension = ext; } };
    const window = { comfyAPI: {} };
    if (withPreviewApi) {
        window.comfyAPI.textPreviewWidgets = {
            addTextPreviewWidgets(node) { calls.push(["add", node.id]); node.widgets.push({ name: "preview_text", value: "" }); },
            updateTextPreviewWidgets(node, output) { calls.push(["update", node.id, output?.text]); },
        };
    } else {
        window.comfyAPI.widgets = { ComfyWidgets: { STRING(node, name, spec) {
            const widget = { name, value: "", inputEl: {}, options: {}, serialize: true, spec };
            node.widgets.push(widget);
            return { widget };
        } } };
    }
    // A root graph with one node 3, and a subgraph "sub-1" holding node 7.
    const makeNode = (id, type) => ({ id, type, comfyClass: type, widgets: [], size: [200, 66],
        setSize(size) { this.size = size; } });
    const rootNode = makeNode(3, "SonderTemplateHelper");
    const subNode = makeNode(7, "SonderTemplateHelper");
    const other = makeNode(9, "PreviewAny");
    const sub = { id: "sub-1", getNodeById: (id) => (id === 7 ? subNode : null) };
    app.rootGraph = { id: "root", getNodeById: (id) => ({ 3: rootNode, 9: other })[id] ?? null };
    const graphsOf = (root) => [root, sub];
    new Function("app", "graphsOf", "window", source)(app, graphsOf, window);

    // The node class, as ComfyUI registers it.
    function NodeType() {}
    NodeType.prototype.onNodeCreated = function () { calls.push(["original created", this.id]); };
    NodeType.prototype.onExecuted = function () { calls.push(["original executed", this.id]); };
    const other_ = { prototype: {} };
    extension.beforeRegisterNodeDef(other_, { name: "SomethingElse" });
    extension.beforeRegisterNodeDef(NodeType, { name: "SonderTemplateHelper" });
    const node = Object.assign(Object.create(NodeType.prototype), makeNode(5, "SonderTemplateHelper"));
    node.onNodeCreated();
    node.onExecuted({ text: ["line one\nline two"] });
    extension.onNodeOutputsUpdated({ "3": { text: ["root report"] }, "sub-1:7": { text: ["sub report"] },
                                     "9": { text: ["not ours"] }, "missing:1": { text: ["x"] } });
    return { name: extension.name, calls, untouched: Object.keys(other_.prototype),
             size: node.size, widgets: node.widgets.map((w) => ({ name: w.name, value: w.value,
             serialize: w.serialize, readOnly: w.inputEl?.readOnly ?? null })),
             rootWidgets: rootNode.widgets, subWidgets: subNode.widgets };
}
console.log(JSON.stringify({ api: run(true), fallback: run(false) }));
"""


def _run():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for browser module tests")
    script = _SCRIPT.replace("__SOURCE__", json.dumps(str(ROOT / "web" / "js" / "template_helper.js")))
    completed = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def test_with_the_frontends_preview_widget_the_report_follows_runs_and_restored_outputs():
    api = _run()["api"]
    assert api["name"] == "Sonder.TemplateHelper" and api["untouched"] == []
    assert api["calls"] == [
        ["original created", 5], ["add", 5],
        ["original executed", 5], ["update", 5, ["line one\nline two"]],
        # Restored outputs: the root node and the subgraph node; another node type is left alone.
        ["update", 3, ["root report"]], ["update", 7, ["sub report"]],
    ]
    assert api["size"] == [460, 300]  # room for the report; a saved size is applied after this


def test_without_it_a_read_only_unsaved_text_box_shows_the_report():
    fallback = _run()["fallback"]
    assert fallback["widgets"] == [
        {"name": "report_preview", "value": "line one\nline two", "serialize": False, "readOnly": True}]
    # Nodes created before the extension (restored outputs) have no box: nothing to update, no error.
    assert fallback["rootWidgets"] == [] and fallback["subWidgets"] == []
