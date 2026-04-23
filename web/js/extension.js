const { app } = window.comfyAPI.app;
const { api } = window.comfyAPI.api;

import { EditorNodeController } from "./editor_node_controller.js";
import { getEditorSettings } from "./editor_settings.js";

function style(el, cssText) {
    el.style.cssText = cssText;
    return el;
}

// ── Widget hide/show helpers ───────────────────────────────────────────
function hideWidget(node, widget) {
    if (widget.hidden) return;
    widget.hidden = true;
    widget._sonder_origComputeSize = widget.computeSize;
    widget.computeSize = () => [0, -4];
}

function showWidget(node, widget) {
    if (!widget.hidden) return;
    widget.hidden = false;
    if (widget._sonder_origComputeSize) {
        widget.computeSize = widget._sonder_origComputeSize;
        delete widget._sonder_origComputeSize;
    } else {
        delete widget.computeSize;
    }
}

// ── Utility: get project directory from project dropdown value ─────────
async function getProjectDir(projectValue) {
    if (!projectValue || projectValue === "+ Create New") return "";
    try {
        const resp = await fetch(api.apiURL("/sonder-editor/projects"));
        if (!resp.ok) return "";

        const data = await resp.json();
        const match = (data.projects || []).find((project) => {
            const dirName = project.path.split(/[/\\]/).pop();
            return dirName === projectValue || project.name === projectValue;
        });
        return match ? match.path : "";
    } catch (e) {
        console.warn("[Sonder] Failed to get project dir:", e);
    }
    return "";
}

async function listProjects() {
    const resp = await fetch(api.apiURL("/sonder-editor/projects"));
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

function normalizeFolderValue(value) {
    return String(value || "").trim().replace(/\\/g, "/").replace(/^\/+/, "").replace(/\/+$/, "");
}

function uniqueFolderValues(values) {
    const seen = new Set();
    const result = [];
    for (const value of values || []) {
        const normalized = normalizeFolderValue(value);
        if (seen.has(normalized)) continue;
        seen.add(normalized);
        result.push(normalized);
    }
    return result;
}

function projectIdFromProjectValue(projectValue) {
    const value = String(projectValue || "").trim();
    if (!value || value === "+ Create New") return "";
    return value.split(/[/\\]/).pop();
}

async function listProjectAssetFolders(projectId) {
    if (!projectId) return [];
    const resp = await fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(projectId)}/assets/dormant`));
    if (!resp.ok) {
        throw new Error(`Failed to list asset folders: ${resp.status}`);
    }
    const data = await resp.json();
    return uniqueFolderValues(data?.folders || []).filter(Boolean);
}

async function createProjectFromNode(node, projectWidget) {
    const projectNameWidget = node.widgets.find((widget) => widget.name === "project_name");
    const fpsWidget = node.widgets.find((widget) => widget.name === "fps");
    const widthWidget = node.widgets.find((widget) => widget.name === "width");
    const heightWidget = node.widgets.find((widget) => widget.name === "height");
    const settings = getEditorSettings();
    const defaultSceneDuration = Math.max(1, Number(settings?.projectDefaults?.newSceneDuration || 200));

    const projectName = String(projectNameWidget?.value || "").trim();
    if (!projectName) {
        throw new Error("Project name is required");
    }

    const resp = await fetch(api.apiURL("/sonder-editor/project"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            name: projectName,
            fps: Number(fpsWidget?.value || 24),
            width: Number(widthWidget?.value || 768),
            height: Number(heightWidget?.value || 512),
            template_id: settings.projectDefaults.defaultTemplateId || "free",
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
    const nextValue = createdProject?.path?.split(/[/\\]/).pop();

    if (!nextValue) {
        throw new Error("Created project was not found after refreshing the project list");
    }

    if ((Number(createdProject.scene_count) || 0) <= 0) {
        const sceneResp = await fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(nextValue)}/scenes`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                name: "Scene 1",
                duration_frames: defaultSceneDuration,
            }),
        });
        if (!sceneResp.ok) {
            let message = `Initial scene creation failed: ${sceneResp.status}`;
            try {
                const data = await sceneResp.json();
                if (data?.error) message = data.error;
            } catch {}
            throw new Error(message);
        }
    }

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

function getActiveEditorNodes() {
    return (app.graph._nodes || app.graph.nodes || []).filter(
        (node) => node.type === "SonderEditor" && node._sonderController?.state?.projectDir
    );
}

function getGraphLinks() {
    return app.graph?.links || app.graph?._links || {};
}

function getNodeById(nodeId) {
    if (nodeId == null) return null;
    if (typeof app.graph?.getNodeById === "function") {
        return app.graph.getNodeById(nodeId) || null;
    }
    return (app.graph?._nodes || app.graph?.nodes || []).find((node) => node.id === nodeId) || null;
}

function getLinkedNodeFromInput(node, inputName) {
    const input = node?.inputs?.find?.((entry) => entry?.name === inputName);
    const linkId = input?.link;
    if (linkId == null) return null;
    const link = getGraphLinks()?.[linkId];
    if (link?.origin_id == null) return null;
    return getNodeById(link.origin_id);
}

function collectUpstreamEditorNodes(startNode, collected = new Set(), visited = new Set()) {
    if (!startNode || visited.has(startNode.id)) return collected;
    visited.add(startNode.id);
    if (startNode.type === "SonderEditor") {
        collected.add(startNode);
        return collected;
    }
    for (const input of startNode.inputs || []) {
        if (input?.link == null) continue;
        const link = getGraphLinks()?.[input.link];
        if (link?.origin_id == null) continue;
        const upstreamNode = getNodeById(link.origin_id);
        if (upstreamNode) {
            collectUpstreamEditorNodes(upstreamNode, collected, visited);
        }
    }
    return collected;
}

function editorNodeHasQueuedWork(node) {
    const counts = node?._sonderController?.state?.dormantSummary?.queue_counts || {};
    return (counts.pending || 0) > 0 || (counts.running || 0) > 0;
}

function refreshEditorNodes(editorNodes) {
    for (const editorNode of editorNodes || []) {
        editorNode._sonderController.handleSaveVideoExecuted();
    }
}

function getSaveVideoEditorNodes(saveNode) {
    const projectSourceNode = getLinkedNodeFromInput(saveNode, "project");
    if (!projectSourceNode) return [];
    return Array.from(collectUpstreamEditorNodes(projectSourceNode)).filter(
        (node) => node._sonderController?.state?.projectDir
    );
}

function getSaveBridgeEditorNodes(bridgeNode) {
    const projectSourceNode = getLinkedNodeFromInput(bridgeNode, "project");
    if (!projectSourceNode) return [];
    return Array.from(collectUpstreamEditorNodes(projectSourceNode));
}

function getEditorProjectId(editorNode) {
    const projectDir = editorNode?._sonderController?.state?.projectDir || "";
    if (projectDir) return projectDir.split(/[/\\]/).pop();
    const projectWidget = editorNode?.widgets?.find((widget) => widget.name === "project");
    return projectIdFromProjectValue(projectWidget?.value);
}

function getSaveBridgeProjectId(bridgeNode) {
    for (const editorNode of getSaveBridgeEditorNodes(bridgeNode)) {
        const projectId = getEditorProjectId(editorNode);
        if (projectId) return projectId;
    }
    return "";
}

function buildBridgeFolderOptions(folders, currentValue = "") {
    const values = [""];
    const current = normalizeFolderValue(currentValue);
    if (current) values.push(current);
    for (const folder of uniqueFolderValues(folders).filter(Boolean)) {
        if (!values.includes(folder)) {
            values.push(folder);
        }
    }
    return values;
}

function setBridgeFolderWidgetChoices(folderWidget, folders, currentValue = "") {
    if (!folderWidget) return [];
    const values = buildBridgeFolderOptions(folders, currentValue);
    folderWidget.options = folderWidget.options || {};
    folderWidget.options.values = values;
    folderWidget.options.editable = true;
    return values;
}

function renderBridgeFolderSuggestions(node, values) {
    const datalist = node?._sonderBridgeFolderDatalist;
    if (!datalist) return;
    datalist.innerHTML = "";
    for (const folder of values.filter(Boolean)) {
        const option = document.createElement("option");
        option.value = folder;
        datalist.appendChild(option);
    }
}

async function syncBridgeTargetFolderWidget(node) {
    const folderWidget = node?.widgets?.find((widget) => widget.name === "target_folder");
    const input = node?._sonderBridgeFolderInput;
    if (!folderWidget || !input) return [];

    const syncToken = (Number(node._sonderBridgeFolderSyncToken) || 0) + 1;
    node._sonderBridgeFolderSyncToken = syncToken;

    const projectId = getSaveBridgeProjectId(node);
    let folders = [];
    if (projectId) {
        try {
            folders = await listProjectAssetFolders(projectId);
        } catch (error) {
            console.warn("[Sonder] Failed to load bridge folder suggestions:", error);
        }
    }
    if (syncToken !== node._sonderBridgeFolderSyncToken) return [];

    const currentValue = normalizeFolderValue(input.value || folderWidget.value || "");
    const values = setBridgeFolderWidgetChoices(folderWidget, folders, currentValue);
    renderBridgeFolderSuggestions(node, values);
    input.placeholder = projectId
        ? "Root (blank) or folder label"
        : "Connect a Sonder project to load folder suggestions";
    input.value = currentValue;
    return values;
}

function installBridgeFolderPicker(node) {
    if (!node?.widgets || node._sonderBridgeFolderInput || typeof node.addDOMWidget !== "function") return;
    const folderWidget = node.widgets.find((widget) => widget.name === "target_folder");
    if (!folderWidget) return;

    hideWidget(node, folderWidget);
    setBridgeFolderWidgetChoices(folderWidget, [], folderWidget.value || "");

    const wrapper = style(document.createElement("div"), `
        display:flex;
        flex-direction:column;
        gap:4px;
        width:100%;
        box-sizing:border-box;
        padding-top:2px;
    `);

    const label = style(document.createElement("div"), `
        color:#cfd7df;
        font-size:10px;
        font-weight:600;
        line-height:1.2;
    `);
    label.textContent = "Target Folder";

    const input = style(document.createElement("input"), `
        width:100%;
        box-sizing:border-box;
        padding:4px 6px;
        border-radius:6px;
        border:1px solid rgba(126, 168, 201, 0.35);
        background:rgba(14, 19, 25, 0.92);
        color:#e5edf5;
        font-size:11px;
        outline:none;
    `);
    const datalist = document.createElement("datalist");
    datalist.id = `sonder-bridge-folders-${Math.random().toString(36).slice(2)}`;
    input.setAttribute("list", datalist.id);

    const help = style(document.createElement("div"), `
        color:#7f8d9b;
        font-size:10px;
        line-height:1.25;
    `);
    help.textContent = "Blank = Root. Existing folders appear here; typing a new label creates it on registration.";

    wrapper.append(label, input, datalist, help);

    const folderDomWidget = node.addDOMWidget("sonder_bridge_folder_picker", "SonderBridgeFolderPicker", wrapper, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => 56,
        getMaxHeight: () => 56,
        getHeight: () => 56,
    });
    folderDomWidget.computeSize = (width) => [width, 56];

    const applyValue = (value, { fireCallback = false } = {}) => {
        const normalized = normalizeFolderValue(value);
        folderWidget.value = normalized;
        input.value = normalized;
        if (fireCallback) {
            folderWidget.callback?.call(folderWidget, normalized);
        }
        app.graph.setDirtyCanvas?.(true, true);
    };

    input.value = normalizeFolderValue(folderWidget.value || "");
    input.addEventListener("input", () => {
        folderWidget.value = normalizeFolderValue(input.value);
        app.graph.setDirtyCanvas?.(true, true);
    });
    input.addEventListener("change", () => applyValue(input.value, { fireCallback: true }));
    input.addEventListener("blur", () => applyValue(input.value, { fireCallback: true }));
    input.addEventListener("focus", () => {
        void syncBridgeTargetFolderWidget(node);
    });

    node._sonderBridgeFolderInput = input;
    node._sonderBridgeFolderDatalist = datalist;

    const origOnConnectionsChange = node.onConnectionsChange;
    node.onConnectionsChange = function () {
        const result = origOnConnectionsChange?.apply(this, arguments);
        void syncBridgeTargetFolderWidget(this);
        return result;
    };

    void syncBridgeTargetFolderWidget(node);
}

const pendingBridgeEditorNodeIds = new Set();
let queuedExecutionRefreshToken = 0;

function trackBridgeExecution(bridgeNode) {
    for (const editorNode of getSaveBridgeEditorNodes(bridgeNode)) {
        if (!editorNode?._sonderController?.state?.projectDir) continue;
        pendingBridgeEditorNodeIds.add(editorNode.id);
    }
}

function getTrackedBridgeEditorNodes() {
    const tracked = [];
    for (const nodeId of [...pendingBridgeEditorNodeIds]) {
        const node = getNodeById(nodeId);
        if (!node?._sonderController?.state?.projectDir) {
            pendingBridgeEditorNodeIds.delete(nodeId);
            continue;
        }
        tracked.push(node);
    }
    return tracked;
}

function schedulePostPromptRefresh() {
    const token = ++queuedExecutionRefreshToken;
    const delays = [0, 150, 400, 1000, 2500, 5000];
    delays.forEach((delay, index) => {
        window.setTimeout(async () => {
            if (token !== queuedExecutionRefreshToken) return;
            const bridgeNodes = getTrackedBridgeEditorNodes();
            const bridgeNodeIds = new Set(bridgeNodes.map((node) => node.id));
            const queueNodes = getActiveEditorNodes().filter(editorNodeHasQueuedWork);
            const editorNodes = Array.from(new Map(
                [...bridgeNodes, ...queueNodes].map((node) => [node.id, node])
            ).values());
            if (!editorNodes.length) {
                if (index === delays.length - 1) {
                    pendingBridgeEditorNodeIds.clear();
                }
                if (token === queuedExecutionRefreshToken && !pendingBridgeEditorNodeIds.size) {
                    queuedExecutionRefreshToken += 1;
                }
                return;
            }
            const counts = await Promise.all(editorNodes.map(async (editorNode) => {
                try {
                    if (bridgeNodeIds.has(editorNode.id)) {
                        return await editorNode._sonderController?.handleBridgeExecutionSettled?.({
                            allowRollback: index === delays.length - 1,
                            attemptIndex: index,
                            delay,
                        });
                    }
                    return await editorNode._sonderController?.handleQueueExecutionSettled?.({
                        allowRollback: index === delays.length - 1,
                        attemptIndex: index,
                        delay,
                    });
                } catch (error) {
                    console.warn("[Sonder] Queue reconciliation failed:", error);
                    return null;
                }
            }));
            if (token !== queuedExecutionRefreshToken) return;
            const hasRunning = counts.some((value) => (value?.running || 0) > 0);
            if (index === delays.length - 1) {
                pendingBridgeEditorNodeIds.clear();
            }
            if (!hasRunning && !pendingBridgeEditorNodeIds.size) {
                queuedExecutionRefreshToken += 1;
            }
        }, delay);
    });
}

// ── Main Extension ─────────────────────────────────────────────────────
app.registerExtension({
    name: "sonder.editor",

    async beforeRegisterNodeDef(nodeType, nodeData, app) {

        // ── Sonder Editor Node ────────────────────────────────────────────
        if (nodeData.name === "SonderEditor") {
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
                node._sonderController = controller;
                node.resizable = true;
                node.flags = { ...(node.flags || {}), resizable: true };
                controller.render();

                const editorDOMWidget = node.addDOMWidget("sonder_editor_ui", "SonderEditorWidget", controller.getElement(), {
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
                        console.warn("[Sonder] Failed to create project:", e);
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
                    if (!node._sonderInitializedSize) {
                        node._sonderInitializedSize = true;
                        node._sonderPreferredWidthMode = modeKey;
                        node.size = [preferredWidth, Math.max(nextSize?.[1] || 0, node.size?.[1] || 0)];
                    } else {
                        if (node._sonderPreferredWidthMode !== modeKey && (node.size?.[0] || 0) < preferredWidth) {
                            node._sonderPreferredWidthMode = modeKey;
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
                        console.warn("[Sonder] Failed to update node visibility:", e);
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
                            console.warn("[Sonder] Failed to sync project choices:", e);
                        });
                }

                // Initial setup
                runUpdateVisibility();

                // Re-render timeline when node resizes
                const origOnResize = node.onResize;
                node.onResize = function () {
                    origOnResize?.apply(this, arguments);
                    node._sonderController?.handleNodeResize?.();
                };

                const origOnRemoved = node.onRemoved;
                node.onRemoved = function () {
                    node._sonderController?.destroy();
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
                    if (!node._sonderController?.state?.projectDir) return false;
                    if (!e.dataTransfer?.files?.length) return false;

                    e.preventDefault?.();
                    e.stopPropagation?.();

                    await node._sonderController.importFiles(e.dataTransfer.files);
                    return true;
                };
            };

            // Handle executed results — refresh editor after execution
            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function () {
                origOnExecuted?.apply(this, arguments);
                this._sonderController?.handleNodeExecuted();
            };
        }

        // ── Sonder Save Video — notify editor nodes to refresh ───────────
        if (nodeData.name === "SonderSaveVideo") {
            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function () {
                origOnExecuted?.apply(this, arguments);
                const editorNodes = getSaveVideoEditorNodes(this);
                refreshEditorNodes(editorNodes);
            };
        }

        if (nodeData.name === "SonderSaveBridge") {
            const origOnNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                origOnNodeCreated?.apply(this, arguments);
                try {
                    installBridgeFolderPicker(this);
                } catch (error) {
                    console.warn("[Sonder] Failed to install bridge folder picker:", error);
                }
            };

            const origOnExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function () {
                origOnExecuted?.apply(this, arguments);
                trackBridgeExecution(this);
            };
        }

    },

    setup() {
        if (typeof api.addEventListener === "function") {
            api.addEventListener("status", (event) => {
                const remaining = Number(event?.detail?.exec_info?.queue_remaining);
                if (!Number.isFinite(remaining) || remaining !== 0) return;
                if (!getActiveEditorNodes().some(editorNodeHasQueuedWork) && !pendingBridgeEditorNodeIds.size) return;
                schedulePostPromptRefresh();
            });
        }
        // ── Global drop interceptor: asset gallery → ComfyUI graph ───────
        // HTML5 drag can't carry File objects, so we intercept drops with our
        // custom MIME type, fetch the actual asset, upload it to ComfyUI's
        // input dir, and create the appropriate loader node.
        document.addEventListener("drop", async (e) => {
            const assetData = e.dataTransfer.getData("application/x-sonder-asset");
            if (!assetData) return; // Not our drag

            // Don't intercept drops on our own editor elements
            if (e.target.closest?.("[data-sonder-editor]")) return;

            e.preventDefault();
            e.stopPropagation();

            try {
                const asset = JSON.parse(assetData);
                const dirName = asset._projectDir;
                if (!dirName) return;
                if (asset.asset_type === "artifact") return;

                const fn = asset.path.split(/[/\\]/).pop();
                const sf = `sonder-projects/${dirName}/${asset.path.split(/[/\\]/).slice(0, -1).join("/")}`;
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
                formData.append("subfolder", "sonder_assets");
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
                    console.warn("[Sonder] No suitable node type for:", asset.asset_type);
                    return;
                }

                // Create node at drop position on the graph
                const graphCanvas = app.canvas;
                const pos = graphCanvas.convertEventToCanvasOffset(e);
                const node = LiteGraph.createNode(nodeType);
                if (!node) {
                    console.warn("[Sonder] Could not create node:", nodeType);
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
                console.warn("[Sonder] ComfyUI graph drop failed:", err);
            }
        }, false); // bubble phase — timeline canvas stopPropagation prevents this from firing for timeline drops
    },
});
