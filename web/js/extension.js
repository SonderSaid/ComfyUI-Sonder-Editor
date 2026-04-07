const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

import { EditorNodeController } from "./editor_node_controller.js";
import { getEditorSettings } from "./editor_settings.js";

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
        if (!resp.ok) return "";

        const data = await resp.json();
        const match = (data.projects || []).find((project) => {
            const dirName = project.path.split(/[/\\]/).pop();
            return dirName === projectValue || project.name === projectValue;
        });
        return match ? match.path : "";
    } catch (e) {
        console.warn("[LTX Editor] Failed to get project dir:", e);
    }
    return "";
}

async function listProjects() {
    const resp = await fetch(api.apiURL("/ltx-editor/projects"));
    if (!resp.ok) {
        throw new Error(`Failed to list projects: ${resp.status}`);
    }
    const data = await resp.json();
    return data.projects || [];
}

async function syncProjectWidgetChoices(projectWidget) {
    if (!projectWidget) return [];
    const projects = await listProjects();
    const values = ["+ Create New", ...projects.map((project) => project.path.split(/[/\\]/).pop())];
    projectWidget.options = projectWidget.options || {};
    projectWidget.options.values = values;
    return projects;
}

async function createProjectFromNode(node, projectWidget) {
    const projectNameWidget = node.widgets.find((widget) => widget.name === "project_name");
    const fpsWidget = node.widgets.find((widget) => widget.name === "fps");
    const widthWidget = node.widgets.find((widget) => widget.name === "width");
    const heightWidget = node.widgets.find((widget) => widget.name === "height");

    const projectName = String(projectNameWidget?.value || "").trim();
    if (!projectName) {
        throw new Error("Project name is required");
    }

    const resp = await fetch(api.apiURL("/ltx-editor/project"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            name: projectName,
            fps: Number(fpsWidget?.value || 24),
            width: Number(widthWidget?.value || 768),
            height: Number(heightWidget?.value || 512),
        }),
    });
    if (!resp.ok) {
        let message = `Project creation failed: ${resp.status}`;
        try {
            const data = await resp.json();
            if (data?.error) message = data.error;
        } catch {}
        throw new Error(message);
    }

    const created = await resp.json();
    const projects = await syncProjectWidgetChoices(projectWidget);
    const createdProject = projects.find((project) => project.project_id === created.project_id)
        || projects.find((project) => project.name === created.name);
    const nextValue = createdProject
        ? createdProject.path.split(/[/\\]/).pop()
        : created.name;

    projectWidget.value = nextValue;
    projectWidget.callback?.(nextValue);
    app.graph.setDirtyCanvas?.(true, true);
}

function applyProjectCreationDefaults(node) {
    if (!node?.widgets) return;
    const settings = getEditorSettings();
    const fpsWidget = node.widgets.find((widget) => widget.name === "fps");
    const widthWidget = node.widgets.find((widget) => widget.name === "width");
    const heightWidget = node.widgets.find((widget) => widget.name === "height");
    if (fpsWidget) fpsWidget.value = settings.projectDefaults.fps;
    if (widthWidget) widthWidget.value = settings.projectDefaults.width;
    if (heightWidget) heightWidget.value = settings.projectDefaults.height;
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
                const projectWidget = node.widgets.find((widget) => widget.name === "project");
                const creationWidgetNames = ["project_name", "fps", "width", "height"];
                const hiddenWidgetNames = [
                    "scene_id",
                    "selection_start",
                    "selection_end",
                    "pre_context_frames",
                    "post_context_frames",
                ];

                // Store original types
                for (const widget of node.widgets) {
                    widget._origType = widget.type;
                }

                const controller = new EditorNodeController(node, projectWidget);
                node._ltxController = controller;
                node.resizable = true;
                node.flags = { ...(node.flags || {}), resizable: true };
                controller.render();

                const editorDOMWidget = node.addDOMWidget("ltx_editor_ui", "LTXEditorWidget", controller.getElement(), {
                    serialize: false,
                    hideOnZoom: false,
                    getMinHeight: () => 150,
                    getMaxHeight: () => controller.getHeight(),
                    getHeight: () => controller.getHeight(),
                });
                editorDOMWidget.computeSize = (width) => [width, controller.getHeight()];

                // Override node.computeSize to allow shrinking during interactive resize.
                // Widget computeSize returns _height (correct for layout), but node.computeSize
                // replaces the widget's contribution with a fixed 150px floor so LiteGraph
                // doesn't clamp the node at _height + overhead.
                const origNodeComputeSize = node.computeSize.bind(node);
                node.computeSize = function () {
                    const result = origNodeComputeSize();
                    const widgetHeight = controller.getHeight();
                    const overhead = result[1] - widgetHeight;
                    result[1] = 150 + overhead;
                    return result;
                };

                const createButtonWidget = node.addWidget("button", "Create", null, async () => {
                    try {
                        await createProjectFromNode(node, projectWidget);
                    } catch (e) {
                        console.warn("[LTX Editor] Failed to create project:", e);
                    }
                });
                createButtonWidget.serialize = false;

                const updateVisibility = async () => {
                    const isCreateNew = projectWidget?.value === "+ Create New";
                    for (const widget of node.widgets) {
                        if (creationWidgetNames.includes(widget.name)) {
                            if (isCreateNew) {
                                showWidget(node, widget);
                            } else {
                                hideWidget(node, widget);
                            }
                        }
                        if (hiddenWidgetNames.includes(widget.name)) {
                            hideWidget(node, widget);
                        }
                    }
                    if (isCreateNew) {
                        applyProjectCreationDefaults(node);
                        showWidget(node, createButtonWidget);
                    } else {
                        hideWidget(node, createButtonWidget);
                    }
                    app.graph.setDirtyCanvas?.(true, true);

                    if (isCreateNew) {
                        hideWidget(node, editorDOMWidget);
                        controller.getElement().style.display = "none";
                        await controller.updateProject("", projectWidget?.value || "");
                    } else {
                        showWidget(node, editorDOMWidget);
                        controller.getElement().style.display = "";
                        const dir = await getProjectDir(projectWidget?.value);
                        await controller.updateProject(dir, projectWidget?.value || "");
                    }

                    app.graph.setDirtyCanvas?.(true, true);

                    const nextSize = node.computeSize();
                    const preferredWidth = isCreateNew ? 340 : 440;
                    const modeKey = isCreateNew ? "create" : "existing";
                    if (!node._ltxInitializedSize) {
                        node._ltxInitializedSize = true;
                        node._ltxPreferredWidthMode = modeKey;
                        node.size = [preferredWidth, Math.max(nextSize?.[1] || 0, node.size?.[1] || 0)];
                    } else {
                        if (node._ltxPreferredWidthMode !== modeKey && (node.size?.[0] || 0) < preferredWidth) {
                            node._ltxPreferredWidthMode = modeKey;
                            node.size = [preferredWidth, node.size?.[1] || nextSize?.[1] || controller.getHeight()];
                        }
                        node.size = [
                            Math.max(node.size?.[0] || 0, 240),
                            nextSize?.[1] || node.size?.[1] || controller.getHeight(),
                        ];
                    }
                    node.setSize(node.size);

                    if (!isCreateNew) {
                        controller.queueResize();
                    }
                };

                const runUpdateVisibility = () => {
                    Promise.resolve(updateVisibility()).catch((e) => {
                        console.warn("[LTX Editor] Failed to update node visibility:", e);
                    });
                };

                // Hook dropdown changes
                if (projectWidget) {
                    const origCallback = projectWidget.callback;
                    projectWidget.callback = (value) => {
                        origCallback?.call(projectWidget, value);
                        runUpdateVisibility();
                    };
                    syncProjectWidgetChoices(projectWidget)
                        .then(() => runUpdateVisibility())
                        .catch((e) => {
                            console.warn("[LTX Editor] Failed to sync project choices:", e);
                        });
                }

                // Initial setup
                runUpdateVisibility();

                // Re-render timeline when node resizes
                const origOnResize = node.onResize;
                node.onResize = function () {
                    origOnResize?.apply(this, arguments);
                    node._ltxController?.handleNodeResize?.();
                };

                const origOnRemoved = node.onRemoved;
                node.onRemoved = function () {
                    node._ltxController?.destroy();
                    origOnRemoved?.apply(this, arguments);
                };

                // Drag-and-drop files onto the node → import to project
                // Must stopPropagation to prevent ComfyUI from intercepting
                node.onDragOver = function (e) {
                    if (e.dataTransfer && e.dataTransfer.items) {
                        const hasFiles = [...e.dataTransfer.items].some(
                            (f) => f.kind === "file"
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
                    if (!node._ltxController?.state?.projectDir) return false;
                    if (!e.dataTransfer?.files?.length) return false;

                    e.preventDefault?.();
                    e.stopPropagation?.();

                    await node._ltxController.importFiles(e.dataTransfer.files);
                    return true;
                };
            };

            // Handle executed results — refresh editor after execution
            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function () {
                origOnExecuted?.apply(this, arguments);
                this._ltxController?.handleNodeExecuted();
            };
        }

        // ── LTX Save Video — notify editor nodes to refresh ───────────
        if (nodeData.name === "LTXSaveVideo") {
            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function () {
                origOnExecuted?.apply(this, arguments);
                const editorNodes = (app.graph._nodes || app.graph.nodes || []).filter(
                    (n) => n.type === "LTXEditor" && n._ltxController?.state?.projectDir
                );
                for (const en of editorNodes) {
                    en._ltxController.handleSaveVideoExecuted();
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
                node.resizable = true;
                node.flags = { ...(node.flags || {}), resizable: true };

                const createButtonWidget = node.addWidget("button", "Create", null, async () => {
                    try {
                        await createProjectFromNode(node, projectWidget);
                    } catch (e) {
                        console.warn("[LTX Project Loader] Failed to create project:", e);
                    }
                });
                createButtonWidget.serialize = false;

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
                    if (isCreateNew) {
                        applyProjectCreationDefaults(node);
                        showWidget(node, createButtonWidget);
                    } else {
                        hideWidget(node, createButtonWidget);
                    }
                    app.graph.setDirtyCanvas?.(true, true);

                    const nextSize = node.computeSize();
                    const preferredWidth = 340;
                    if (!node._ltxInitializedSize) {
                        node._ltxInitializedSize = true;
                        node.size = [preferredWidth, Math.max(nextSize?.[1] || 0, node.size?.[1] || 0)];
                    } else {
                        node.size = [
                            Math.max(node.size?.[0] || 0, 240),
                            nextSize?.[1] || node.size?.[1] || preferredWidth,
                        ];
                    }
                    node.setSize(node.size);
                };

                const runUpdateVisibility = () => {
                    Promise.resolve(updateVisibility()).catch((e) => {
                        console.warn("[LTX Project Loader] Failed to update node visibility:", e);
                    });
                };

                if (projectWidget) {
                    const origCallback = projectWidget.callback;
                    projectWidget.callback = (value) => {
                        origCallback?.call(projectWidget, value);
                        runUpdateVisibility();
                    };
                    syncProjectWidgetChoices(projectWidget)
                        .then(() => runUpdateVisibility())
                        .catch((e) => {
                            console.warn("[LTX Project Loader] Failed to sync project choices:", e);
                        });
                }

                runUpdateVisibility();
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
