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

    setup() {
        // ── Global drop interceptor: asset gallery → ComfyUI graph ───────
        // HTML5 drag can't carry File objects, so we intercept drops with our
        // custom MIME type, fetch the actual asset, upload it to ComfyUI's
        // input dir, and create the appropriate loader node.
        document.addEventListener("drop", async (e) => {
            const assetData = e.dataTransfer.getData("application/ltx-asset");
            if (!assetData) return; // Not our drag

            // Don't intercept drops on our own editor elements
            if (e.target.closest?.("[data-ltx-editor]")) return;

            e.preventDefault();
            e.stopPropagation();

            try {
                const asset = JSON.parse(assetData);
                const dirName = asset._projectDir;
                if (!dirName) return;

                const fn = asset.path.split(/[/\\]/).pop();
                const sf = `ltx_projects/${dirName}/${asset.path.split(/[/\\]/).slice(0, -1).join("/")}`;
                const viewUrl = api.apiURL(
                    `/view?filename=${encodeURIComponent(fn)}&subfolder=${encodeURIComponent(sf)}&type=output`
                );

                // Fetch the actual asset file
                const resp = await fetch(viewUrl);
                if (!resp.ok) throw new Error(`Fetch failed: ${resp.status}`);
                const blob = await resp.blob();
                const file = new File([blob], fn, { type: blob.type });

                // Upload to ComfyUI input directory
                const formData = new FormData();
                formData.append("image", file);
                formData.append("subfolder", "ltx_assets");
                const uploadResp = await api.fetchApi("/upload/image", {
                    method: "POST",
                    body: formData,
                });
                if (!uploadResp.ok) throw new Error(`Upload failed: ${uploadResp.status}`);
                const uploadResult = await uploadResp.json();
                const uploadedName = uploadResult.subfolder
                    ? `${uploadResult.subfolder}/${uploadResult.name}`
                    : uploadResult.name;

                // Pick node type based on asset type
                const regTypes = LiteGraph.registered_node_types || {};
                let nodeType, widgetName;
                if (asset.asset_type === "image") {
                    nodeType = "LoadImage";
                    widgetName = "image";
                } else if (asset.asset_type === "video") {
                    nodeType = regTypes["VHS_LoadVideo"] ? "VHS_LoadVideo" : "LoadImage";
                    widgetName = nodeType === "VHS_LoadVideo" ? "video" : "image";
                } else if (asset.asset_type === "audio") {
                    nodeType = regTypes["LoadAudio"] ? "LoadAudio" : null;
                    widgetName = "audio";
                }

                if (!nodeType) {
                    console.warn("[LTX Editor] No suitable node type for:", asset.asset_type);
                    return;
                }

                // Create node at drop position on the graph
                const graphCanvas = app.canvas;
                const pos = graphCanvas.convertEventToCanvasOffset(e);
                const node = LiteGraph.createNode(nodeType);
                if (!node) {
                    console.warn("[LTX Editor] Could not create node:", nodeType);
                    return;
                }
                node.pos = [pos[0], pos[1]];
                app.graph.add(node);

                // Set the file widget value
                const widget = node.widgets?.find(w => w.name === widgetName);
                if (widget) {
                    widget.value = uploadedName;
                    widget.callback?.(uploadedName);
                }
            } catch (err) {
                console.warn("[LTX Editor] ComfyUI graph drop failed:", err);
            }
        }, false); // bubble phase — timeline canvas stopPropagation prevents this from firing for timeline drops
    },
});
