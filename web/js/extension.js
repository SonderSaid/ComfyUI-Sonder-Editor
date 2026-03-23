const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

import { EditorWidget } from "./editor_widget.js";

// ── Widget hide/show helpers ───────────────────────────────────────────
function hideWidget(node, widget) {
    if (widget.hidden) return;
    widget.hidden = true;
    widget._ltx_origComputeSize = widget.computeSize;
    widget.computeSize = () => [0, -4];
}

function showWidget(node, widget) {
    if (!widget.hidden) return;
    widget.hidden = false;
    if (widget._ltx_origComputeSize) {
        widget.computeSize = widget._ltx_origComputeSize;
        delete widget._ltx_origComputeSize;
    } else {
        delete widget.computeSize;
    }
}

// ── Utility: get project directory from project dropdown value ─────────
async function getProjectDir(projectValue) {
    if (!projectValue || projectValue === "+ Create New") return "";
    try {
        const resp = await fetch(api.apiURL("/ltx-editor/projects"));
        if (resp.ok) {
            const data = await resp.json();
            const match = (data.projects || []).find(p => {
                const dirName = p.path.split(/[/\\]/).pop();
                return dirName === projectValue || p.name === projectValue;
            });
            return match ? match.path : "";
        }
    } catch (e) {
        console.warn("[LTX Editor] Failed to get project dir:", e);
    }
    return "";
}

// ── Main Extension ─────────────────────────────────────────────────────
app.registerExtension({
    name: "ltx.editor",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {

        // ── LTX Editor Node ────────────────────────────────────────────
        if (nodeData.name === "LTXEditor") {
            const origOnNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                origOnNodeCreated?.apply(this, arguments);

                const node = this;
                const projectWidget = this.widgets.find(w => w.name === "project");
                const creationWidgetNames = ["project_name", "fps", "width", "height"];
                const hiddenWidgetNames = ["scene_id", "selection_start", "selection_end"];

                // Store original types
                for (const w of this.widgets) {
                    w._origType = w.type;
                }

                // Create the editor widget
                const editor = new EditorWidget(node);
                node._ltxEditor = editor;

                const editorElement = editor.getElement();
                const editorDOMWidget = node.addDOMWidget("ltx_editor_ui", "LTXEditorWidget", editorElement, {
                    serialize: false,
                    hideOnZoom: false,
                    getMinHeight: () => editor.getHeight(),
                    getMaxHeight: () => editor.getHeight(),
                    getHeight: () => editor.getHeight(),
                });
                editorDOMWidget.computeSize = (width) => [width, editor.getHeight()];

                const updateVisibility = async () => {
                    const isCreateNew = projectWidget?.value === "+ Create New";
                    for (const w of node.widgets) {
                        if (creationWidgetNames.includes(w.name)) {
                            if (isCreateNew) {
                                showWidget(node, w);
                            } else {
                                hideWidget(node, w);
                            }
                        }
                        if (hiddenWidgetNames.includes(w.name)) {
                            hideWidget(node, w);
                        }
                    }

                    // Show/hide editor widget based on mode
                    if (isCreateNew) {
                        hideWidget(node, editorDOMWidget);
                    } else {
                        showWidget(node, editorDOMWidget);
                        // Load project data into editor
                        const dir = await getProjectDir(projectWidget.value);
                        if (dir) {
                            editor.updateProject(dir);
                        }
                    }

                    node.setSize(node.computeSize());
                };

                // Hook dropdown changes
                if (projectWidget) {
                    const origCallback = projectWidget.callback;
                    projectWidget.callback = (value) => {
                        origCallback?.call(projectWidget, value);
                        updateVisibility();
                    };
                }

                // Initial setup
                setTimeout(() => updateVisibility(), 100);

                // Re-render timeline when node resizes
                const origOnResize = node.onResize;
                node.onResize = function (size) {
                    origOnResize?.apply(this, arguments);
                    if (node._ltxEditor) {
                        setTimeout(() => node._ltxEditor._renderTimeline(), 50);
                    }
                };

                // Drag-and-drop files onto the node → import to project
                // Must stopPropagation to prevent ComfyUI from intercepting
                node.onDragOver = function (e) {
                    if (e.dataTransfer && e.dataTransfer.items) {
                        const hasFiles = [...e.dataTransfer.items].some(
                            f => f.kind === "file"
                        );
                        if (hasFiles) {
                            e.preventDefault?.();
                            e.stopPropagation?.();
                            return true;
                        }
                    }
                    return false;
                };

                node.onDragDrop = async (e) => {
                    if (!node._ltxEditor || !node._ltxEditor.projectDir) return false;
                    if (!e.dataTransfer?.files?.length) return false;

                    e.preventDefault?.();
                    e.stopPropagation?.();

                    for (const file of e.dataTransfer.files) {
                        await node._ltxEditor._importFile(file);
                    }
                    return true;
                };
            };

            // Handle executed results — refresh editor after execution
            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (output) {
                origOnExecuted?.apply(this, arguments);
                if (this._ltxEditor && this._ltxEditor.projectDir) {
                    this._ltxEditor._fetchScenes();
                    this._ltxEditor._fetchAssets();
                }
            };
        }

        // ── LTX Save Video — notify editor nodes to refresh ───────────
        if (nodeData.name === "LTXSaveVideo") {
            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (output) {
                origOnExecuted?.apply(this, arguments);
                // Find all LTX Editor nodes and refresh their assets
                const editorNodes = (app.graph._nodes || app.graph.nodes || []).filter(
                    n => n.type === "LTXEditor" && n._ltxEditor?.projectDir
                );
                for (const en of editorNodes) {
                    en._ltxEditor._fetchAssets();
                }
            };
        }

        // ── Legacy LTX Project Loader ──────────────────────────────────
        if (nodeData.name === "LTXProjectLoader") {
            const origOnNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                origOnNodeCreated?.apply(this, arguments);

                const projectWidget = this.widgets.find(w => w.name === "project");
                const creationWidgetNames = ["project_name", "fps", "width", "height"];
                const node = this;

                const updateVisibility = () => {
                    const isCreateNew = projectWidget?.value === "+ Create New";
                    for (const w of node.widgets) {
                        if (creationWidgetNames.includes(w.name)) {
                            if (isCreateNew) {
                                showWidget(node, w);
                            } else {
                                hideWidget(node, w);
                            }
                        }
                    }
                    node.setSize(node.computeSize());
                };

                if (projectWidget) {
                    const origCallback = projectWidget.callback;
                    projectWidget.callback = (value) => {
                        origCallback?.call(projectWidget, value);
                        updateVisibility();
                    };
                }

                setTimeout(() => updateVisibility(), 50);
            };
        }
    },
});
