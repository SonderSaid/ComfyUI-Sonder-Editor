const { api } = window.comfyAPI.api;

import { EditorWidget, buildProjectAssetViewURL, importFileIntoProject, replaceAssetInProject } from "./editor_widget.js";
import { mountSharedAssetGallery } from "./shared_asset_gallery.js";

function style(el, cssText) {
    el.style.cssText = cssText;
    return el;
}

function iconForAssetType(type) {
    if (type === "video") return "🎬";
    if (type === "image") return "🖼";
    if (type === "audio") return "🔊";
    return "•";
}

function projectIdFromDir(projectDir) {
    return projectDir ? projectDir.split(/[/\\]/).pop() : "";
}

function formatCountLabel(prefix, value) {
    return `${prefix}${value || 0}`;
}

function formatDurationFrames(frameCount) {
    if (!Number.isFinite(frameCount) || frameCount <= 0) return "0f";
    return `${frameCount}f`;
}

function formatFrameRange(startFrame, endFrame) {
    const start = Math.max(0, parseInt(startFrame, 10) || 0);
    const end = Math.max(start, parseInt(endFrame, 10) || 0);
    return `${start}-${end}`;
}

function buildDormantSummaryUrl(state) {
    const projectId = projectIdFromDir(state.projectDir);
    const params = new URLSearchParams();
    if (state.sceneId) params.set("scene_id", state.sceneId);
    params.set("selection_start", String(state.selectionStart || 0));
    params.set("selection_end", String(state.selectionEnd || 0));
    params.set("pre_context_frames", String(state.preContextFrames || 0));
    params.set("post_context_frames", String(state.postContextFrames || 0));
    return api.apiURL(`/ltx-editor/project/${projectId}/dormant_summary?${params.toString()}`);
}

function buildDormantAssetsUrl(projectDir) {
    return api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/assets/dormant`);
}

function buildQueueUrl(projectDir) {
    return api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/queue`);
}

function buildSceneUrl(projectDir, sceneId) {
    return api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/scenes/${sceneId}`);
}

async function fetchJson(url, signal) {
    const resp = await fetch(url, { signal });
    if (!resp.ok) {
        throw new Error(`Request failed: ${resp.status}`);
    }
    return await resp.json();
}

function isVideoLaneHidden(scene, trackIndex) {
    return !!scene?.video_lane_configs?.[trackIndex || 0]?.hidden;
}

function pickPreviewTarget(projectDir, summary, scene, assets) {
    const activeScene = summary?.active_scene;
    const fallbackFrame = activeScene?.selection?.generation_start_frame || 0;
    const assetsByPath = new Map((assets || []).map(asset => [asset.path, asset]));
    const assetsById = new Map((assets || []).map(asset => [asset.asset_id, asset]));
    const isMissingAsset = (asset) => !asset || !!asset.missing;

    const activeClips = (scene?.clips || [])
        .filter(clip => fallbackFrame >= clip.timeline_start_frame && fallbackFrame < clip.timeline_end_frame)
        .filter(clip => !isVideoLaneHidden(scene, clip.track_index || 0))
        .sort((a, b) => (b.track_index || 0) - (a.track_index || 0));

    if (activeClips.length > 0) {
        const clip = activeClips[0];
        const asset = assetsByPath.get(clip.source_path);
        if (isMissingAsset(asset)) {
            return {
                kind: "missing",
                label: "Missing Video",
                subtitle: asset?.name || clip.source_path.split(/[/\\]/).pop() || "Clip",
            };
        }
        return {
            kind: "video",
            label: `Frame ${fallbackFrame}`,
            subtitle: asset?.name || clip.source_path.split(/[/\\]/).pop() || "Clip",
            posterUrl: asset?.has_thumbnail
                ? api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/thumbnail/${asset.asset_id}`)
                : null,
            mediaUrl: buildProjectAssetViewURL(projectDir, clip.source_path),
        };
    }

    let guide = null;
    let guideFrame = -1;
    for (const item of (scene?.guide_frames || [])) {
        const frameIndex = item.frame_index === -1
            ? Math.max(0, (scene?.duration_frames || 1) - 1)
            : item.frame_index;
        if (frameIndex <= fallbackFrame && frameIndex >= guideFrame) {
            guide = item;
            guideFrame = frameIndex;
        }
    }
    if (guide) {
        const asset = assetsById.get(guide.asset_id);
        if (isMissingAsset(asset)) {
            return {
                kind: "missing",
                label: `Missing Guide ${guideFrame}`,
                subtitle: asset?.name || asset?.path?.split(/[/\\]/).pop() || "Guide asset entry not found.",
            };
        }
        if (asset) {
            return {
                kind: "image",
                label: `Guide ${guideFrame}`,
                subtitle: asset.name || asset.path.split(/[/\\]/).pop() || "Guide",
                posterUrl: buildProjectAssetViewURL(projectDir, asset.path),
            };
        }
    }

    return {
        kind: "empty",
        label: "No preview",
        subtitle: "No clip or guide at the current selection.",
    };
}

class FullscreenEditorSession {
    constructor(controller) {
        this.controller = controller;
        this.editor = null;
        this._destroyed = false;
    }

    mount() {
        if (this._destroyed || !this.controller.state.projectDir) return;

        const state = this.controller.state;
        const editor = new EditorWidget(this.controller.node, {
            onFullscreenExit: () => this._handleEditorClosed(),
            onWidgetValueChange: (name, value) => this.controller.onEditorWidgetValueChange(name, value),
        });

        this.editor = editor;
        editor.updateProject(state.projectDir);
        editor.activeSceneId = state.sceneId || "";
        editor.selectionStart = state.selectionStart || 0;
        editor.selectionEnd = state.selectionEnd || 0;
        editor.playhead = state.selectionStart || 0;
        this.controller._setWidgetValue("pre_context_frames", state.preContextFrames || 0);
        this.controller._setWidgetValue("post_context_frames", state.postContextFrames || 0);
        editor._refreshContextInputs();
        editor.refresh(["queue"]);
        editor._enterFullscreen();
    }

    refresh(keys = []) {
        this.editor?.refresh(keys);
    }

    _handleEditorClosed() {
        this.destroy(true);
    }

    destroy(fromEditor = false) {
        if (this._destroyed) return;
        this._destroyed = true;

        const editor = this.editor;
        this.editor = null;

        if (editor) {
            if (!fromEditor && editor.isFullscreen) {
                const exitCallback = editor.onFullscreenExit;
                editor.onFullscreenExit = null;
                editor._exitFullscreen();
                editor.onFullscreenExit = exitCallback;
            }
            editor.destroy();
        }

        this.controller.onFullscreenSessionDestroyed(this);
    }
}

class DormantNodeCard {
    constructor(controller) {
        this.controller = controller;
        this.root = style(document.createElement("div"), `
            width: 100%;
            height: 100%;
            min-height: 0;
            box-sizing: border-box;
            display: flex;
            flex-direction: column;
            gap: 8px;
            padding: 8px;
            border: 1px solid #3a3a3a;
            border-radius: 8px;
            background: linear-gradient(180deg, #242424 0%, #1b1b1b 100%);
            color: #ddd;
            font-family: 'Segoe UI', Arial, sans-serif;
            font-size: 11px;
            overflow: hidden;
        `);
        this.root.dataset.ltxEditor = "1";

        this._headerEl = this.root.appendChild(document.createElement("div"));
        this._badgeRowEl = this.root.appendChild(style(document.createElement("div"), `
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        `));
        this._metaGridEl = this.root.appendChild(style(document.createElement("div"), `
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 6px;
        `));
        this._countsEl = this.root.appendChild(style(document.createElement("div"), `
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        `));
        this._actionRowEl = this.root.appendChild(style(document.createElement("div"), `
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
        `));
        this._moduleContainerEl = this.root.appendChild(style(document.createElement("div"), `
            display: none;
            flex: 1 1 auto;
            min-height: 0;
            overflow: hidden;
        `));

        this._moduleButtons = {};
        this._mountedModuleId = "";
        this._mountedModuleData = null;
        this._mountedModuleLoading = false;
        this._mountedModuleError = "";
        this._moduleCleanup = null;
    }

    getElement() {
        return this.root;
    }

    teardown() {
        this._teardownModule();
    }

    render() {
        const state = this.controller.state;
        const summary = state.dormantSummary;
        const activeScene = summary?.active_scene;
        const assetCounts = summary?.asset_counts || {};
        const queueCounts = summary?.queue_counts || {};

        this._headerEl.innerHTML = "";
        const titleWrap = style(document.createElement("div"), `
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            gap: 8px;
        `);
        const titleText = style(document.createElement("div"), `
            min-width: 0;
            display: flex;
            flex-direction: column;
            gap: 2px;
        `);
        const projectTitle = style(document.createElement("div"), `
            font-size: 12px;
            font-weight: 700;
            color: #f0f0f0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        `);
        projectTitle.textContent = summary?.name || state.projectName || "LTX Editor";
        const sceneTitle = style(document.createElement("div"), `
            color: #8ba0b3;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        `);
        sceneTitle.textContent = activeScene?.name || (state.projectDir ? "No scene selected" : "Select a project");
        titleText.append(projectTitle, sceneTitle);
        titleWrap.appendChild(titleText);
        this._headerEl.appendChild(titleWrap);

        this._badgeRowEl.innerHTML = "";
        const badges = [];
        if (state.isFullscreenOpen) badges.push({ text: "Editor Active", color: "#284f7a" });
        if ((queueCounts.running || 0) > 0) badges.push({ text: `${queueCounts.running} Running`, color: "#2f5c90" });
        if ((queueCounts.pending || 0) > 0) badges.push({ text: `${queueCounts.pending} Pending`, color: "#5b4d2c" });
        if (!badges.length && summary) badges.push({ text: "Idle", color: "#36453b" });
        for (const badge of badges) {
            const pill = style(document.createElement("span"), `
                padding: 2px 8px;
                border-radius: 999px;
                background: ${badge.color};
                color: #f5f5f5;
                font-size: 10px;
                font-weight: 600;
            `);
            pill.textContent = badge.text;
            this._badgeRowEl.appendChild(pill);
        }

        this._metaGridEl.innerHTML = "";
        const rows = [
            ["Resolution", activeScene ? `${activeScene.effective_width}×${activeScene.effective_height}` : "—"],
            ["FPS", activeScene ? String(activeScene.effective_fps || summary?.fps || 24) : "—"],
            ["Duration", activeScene ? formatDurationFrames(activeScene.duration_frames) : "—"],
            ["Selection", activeScene?.selection?.label || "—"],
        ];
        for (const [label, value] of rows) {
            const cell = style(document.createElement("div"), `
                padding: 6px 8px;
                border-radius: 6px;
                background: rgba(255,255,255,0.04);
                min-width: 0;
            `);
            const keyEl = style(document.createElement("div"), `
                color: #7f8b96;
                font-size: 10px;
                margin-bottom: 2px;
            `);
            keyEl.textContent = label;
            const valueEl = style(document.createElement("div"), `
                color: #ececec;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            `);
            valueEl.textContent = value;
            cell.append(keyEl, valueEl);
            this._metaGridEl.appendChild(cell);
        }

        this._countsEl.innerHTML = "";
        const chips = [
            `Scenes ${summary?.scene_count || 0}`,
            formatCountLabel("V", assetCounts.video),
            formatCountLabel("I", assetCounts.image),
            formatCountLabel("A", assetCounts.audio),
            `Queue ${queueCounts.total || 0}`,
        ];
        for (const text of chips) {
            const chip = style(document.createElement("span"), `
                padding: 3px 7px;
                border-radius: 5px;
                background: rgba(255,255,255,0.05);
                color: #c9d0d6;
            `);
            chip.textContent = text;
            this._countsEl.appendChild(chip);
        }

        this._actionRowEl.innerHTML = "";
        const openBtn = style(document.createElement("button"), `
            background: ${state.isFullscreenOpen ? "#37414a" : "#3a7ca5"};
            color: #fff;
            border: none;
            border-radius: 6px;
            padding: 6px 10px;
            cursor: ${state.projectDir && !state.isFullscreenOpen ? "pointer" : "default"};
            font-size: 11px;
            font-weight: 600;
        `);
        openBtn.textContent = state.isFullscreenOpen ? "Editor Active" : "Open Editor";
        openBtn.disabled = !state.projectDir || state.isFullscreenOpen;
        openBtn.addEventListener("click", () => this.controller.openFullscreen());
        this._actionRowEl.appendChild(openBtn);

        for (const moduleId of ["assets", "preview", "queue"]) {
            const isExpanded = state.expandedModuleId === moduleId;
            const btn = style(document.createElement("button"), `
                background: ${isExpanded ? "#4a5c6b" : "#2b2b2b"};
                color: ${isExpanded ? "#fff" : "#d2d2d2"};
                border: 1px solid ${isExpanded ? "#6f8ea8" : "#444"};
                border-radius: 6px;
                padding: 6px 10px;
                cursor: ${state.projectDir ? "pointer" : "default"};
                font-size: 11px;
            `);
            btn.textContent = moduleId.charAt(0).toUpperCase() + moduleId.slice(1);
            btn.disabled = !state.projectDir;
            btn.addEventListener("click", () => this.controller.toggleModule(moduleId));
            this._actionRowEl.appendChild(btn);
            this._moduleButtons[moduleId] = btn;
        }

        this._renderModuleState();
    }

    _applyModuleContainerSizing(moduleId) {
        style(this._moduleContainerEl, `
            display: flex;
            flex-direction: column;
            flex: 1 1 auto;
            min-height: 0;
            border-top: 1px solid #333;
            padding-top: 8px;
            box-sizing: border-box;
            overflow: hidden;
        `);
    }

    _measureAvailableModuleHeight() {
        const rootRect = this.root.getBoundingClientRect();
        const containerRect = this._moduleContainerEl.getBoundingClientRect();
        if (!rootRect.height) return 0;
        const rootStyle = window.getComputedStyle(this.root);
        const paddingBottom = parseFloat(rootStyle.paddingBottom) || 0;
        return Math.max(0, Math.floor(rootRect.bottom - paddingBottom - containerRect.top));
    }

    syncModuleContainerHeight() {
        const moduleId = this.controller.state.expandedModuleId;
        if (!moduleId || this._moduleContainerEl.style.display === "none") return;
        this._applyModuleContainerSizing(moduleId);
        if (moduleId !== "assets") {
            this._moduleContainerEl.style.height = "";
            this._moduleContainerEl.style.maxHeight = "";
            return;
        }
        const availableHeight = this._measureAvailableModuleHeight();
        this._moduleContainerEl.style.height = availableHeight > 0 ? `${availableHeight}px` : "";
        this._moduleContainerEl.style.maxHeight = availableHeight > 0 ? `${availableHeight}px` : "";
    }

    _renderModuleState() {
        const state = this.controller.state;
        const moduleId = state.expandedModuleId;
        const moduleStatus = moduleId ? this.controller.moduleStatus[moduleId] : null;
        const moduleData = moduleId ? this.controller.moduleCache[moduleId] : null;
        const loading = !!moduleStatus?.loading;
        const error = moduleStatus?.error || "";
        const shouldAutoResizeNode = moduleId && moduleId !== "assets";

        if (!moduleId) {
            this._teardownModule();
            this._moduleContainerEl.style.display = "none";
            this.controller.queueResize();
            return;
        }

        if (!moduleData && !loading && !error) {
            this.controller._loadModule(moduleId);
        }

        const shouldRemount =
            this._mountedModuleId !== moduleId ||
            this._mountedModuleData !== moduleData ||
            this._mountedModuleLoading !== loading ||
            this._mountedModuleError !== error;

        if (!shouldRemount) {
            if (shouldAutoResizeNode) {
                this.controller.queueResize();
            } else {
                this.syncModuleContainerHeight();
            }
            return;
        }

        this._teardownModule();
        this._moduleContainerEl.innerHTML = "";
        this._applyModuleContainerSizing(moduleId);

        if (loading) {
            const loadingEl = style(document.createElement("div"), `
                padding: 10px;
                border-radius: 6px;
                background: rgba(255,255,255,0.04);
                color: #9aa6b2;
            `);
            loadingEl.textContent = "Loading…";
            this._moduleContainerEl.appendChild(loadingEl);
        } else if (error) {
            const errorEl = style(document.createElement("div"), `
                padding: 10px;
                border-radius: 6px;
                background: rgba(120,30,30,0.3);
                color: #ffc9c9;
            `);
            errorEl.textContent = error;
            this._moduleContainerEl.appendChild(errorEl);
        } else if (moduleData) {
            const moduleDef = this.controller.modules[moduleId];
            const cleanup = moduleDef.mount(this._moduleContainerEl, moduleData, this.controller);
            this._moduleCleanup = typeof cleanup === "function" ? cleanup : null;
        }

        this._mountedModuleId = moduleId;
        this._mountedModuleData = moduleData;
        this._mountedModuleLoading = loading;
        this._mountedModuleError = error;
        if (shouldAutoResizeNode) {
            this.controller.queueResize();
        } else {
            this.syncModuleContainerHeight();
        }
    }

    _teardownModule() {
        if (this._moduleCleanup) {
            this._moduleCleanup();
            this._moduleCleanup = null;
        }
        this._mountedModuleId = "";
        this._mountedModuleData = null;
        this._mountedModuleLoading = false;
        this._mountedModuleError = "";
        this._moduleContainerEl.innerHTML = "";
    }
}

export class EditorNodeController {
    constructor(node, projectWidget) {
        this.node = node;
        this.projectWidget = projectWidget;
        this.projectName = projectWidget?.value || "";
        this.moduleCache = {};
        this.state = {
            projectDir: "",
            projectName: projectWidget?.value || "",
            sceneId: this._getWidgetValue("scene_id", ""),
            selectionStart: this._getWidgetValue("selection_start", 0),
            selectionEnd: this._getWidgetValue("selection_end", 0),
            preContextFrames: this._getWidgetValue("pre_context_frames", 0),
            postContextFrames: this._getWidgetValue("post_context_frames", 0),
            dormantSummary: null,
            moduleCache: this.moduleCache,
            isFullscreenOpen: false,
            expandedModuleId: "",
        };
        this.moduleStatus = {
            assets: { loading: false, error: "" },
            preview: { loading: false, error: "" },
            queue: { loading: false, error: "" },
        };
        this._moduleLoadAborters = {};
        this._summaryAborter = null;
        this.fullscreenSession = null;
        this.modules = this._buildModules();
        this.card = new DormantNodeCard(this);
        this.root = this.card.getElement();
        this._height = 190;
        this._resizeScheduled = false;
        this._programmaticResize = false;
        this._destroyed = false;
    }

    destroy() {
        if (this._destroyed) return;
        this._destroyed = true;

        if (this._summaryAborter) {
            this._summaryAborter.abort();
            this._summaryAborter = null;
        }

        for (const moduleId of Object.keys(this._moduleLoadAborters)) {
            this._abortModuleLoad(moduleId);
        }

        this.card.teardown();

        if (this.fullscreenSession) {
            const session = this.fullscreenSession;
            this.fullscreenSession = null;
            session.destroy();
        }
    }

    getElement() {
        return this.root;
    }

    getHeight() {
        return this._height;
    }

    render() {
        if (this._destroyed) return;
        this.card.render();
    }

    handleNodeResize() {
        if (this._destroyed) return;
        if (this._programmaticResize) {
            this.card.syncModuleContainerHeight?.();
            return;
        }
        // User-initiated resize: compute widget height from node height minus overhead
        const nodeHeight = Math.max(0, this.node.size?.[1] || 0);
        if (nodeHeight > 0) {
            const computed = this.node.computeSize?.();
            const totalComputed = computed?.[1] || nodeHeight;
            // 150 matches the widget floor used by the node.computeSize override
            const overhead = Math.max(0, totalComputed - 150);
            this._height = Math.max(150, nodeHeight - overhead);
        }
        this.card.syncModuleContainerHeight?.();
    }

    queueResize() {
        if (this._destroyed || this._resizeScheduled) return;
        this._resizeScheduled = true;
        requestAnimationFrame(() => {
            this._resizeScheduled = false;
            if (this._destroyed) return;
            if (this.root.style.display === "none") return;
            const currentWidth = Math.max(240, this.node.size?.[0] || 0);
            const currentHeight = Math.max(0, this.node.size?.[1] || 0);
            if (this.state.expandedModuleId === "assets") {
                this.card.syncModuleContainerHeight?.();
                return;
            }
            this.card.syncModuleContainerHeight?.();
            const measured = Math.ceil(this.root.scrollHeight || this.root.getBoundingClientRect().height || 190);
            this._height = Math.max(150, measured + 10);
            if (Math.abs(currentHeight - this._height) > 1) {
                this._programmaticResize = true;
                this.node.setSize?.([currentWidth, this._height]);
                this._programmaticResize = false;
            }
        });
    }

    _buildModules() {
        return {
            assets: {
                id: "assets",
                title: "Assets",
                resourceTier: "light",
                load: async (controller, signal) => await controller._loadDormantAssets(signal),
                mount: (container, data, controller) => controller._mountAssetsModule(container, data),
                collapseCleanup: () => {},
                invalidate: (keys) => keys.some(key => key === "project" || key === "assets"),
            },
            preview: {
                id: "preview",
                title: "Preview",
                resourceTier: "media",
                load: async (controller, signal) => await controller._loadPreviewModule(signal),
                mount: (container, data, controller) => controller._mountPreviewModule(container, data),
                collapseCleanup: () => {},
                invalidate: (keys) => keys.some(key => key === "project" || key === "scene" || key === "assets"),
            },
            queue: {
                id: "queue",
                title: "Queue",
                resourceTier: "light",
                load: async (controller, signal) => await controller._loadQueueModule(signal),
                mount: (container, data, controller) => controller._mountQueueModule(container, data),
                collapseCleanup: () => {},
                invalidate: (keys) => keys.some(key => key === "project" || key === "queue"),
            },
        };
    }

    _getWidgetValue(name, defaultValue = 0) {
        const widget = this.node.widgets?.find(w => w.name === name);
        return widget ? widget.value : defaultValue;
    }

    _setWidgetValue(name, value) {
        const widget = this.node.widgets?.find(w => w.name === name);
        if (widget) {
            widget.value = value;
        }
    }

    syncStateFromWidgets() {
        this.state.sceneId = this._getWidgetValue("scene_id", "");
        this.state.selectionStart = Math.max(0, parseInt(this._getWidgetValue("selection_start", 0), 10) || 0);
        this.state.selectionEnd = Math.max(0, parseInt(this._getWidgetValue("selection_end", 0), 10) || 0);
        this.state.preContextFrames = Math.max(0, parseInt(this._getWidgetValue("pre_context_frames", 0), 10) || 0);
        this.state.postContextFrames = Math.max(0, parseInt(this._getWidgetValue("post_context_frames", 0), 10) || 0);
    }

    onEditorWidgetValueChange(name, value) {
        if (name === "scene_id") this.state.sceneId = value || "";
        if (name === "selection_start") this.state.selectionStart = Math.max(0, parseInt(value, 10) || 0);
        if (name === "selection_end") this.state.selectionEnd = Math.max(0, parseInt(value, 10) || 0);
        if (name === "pre_context_frames") this.state.preContextFrames = Math.max(0, parseInt(value, 10) || 0);
        if (name === "post_context_frames") this.state.postContextFrames = Math.max(0, parseInt(value, 10) || 0);
    }

    async updateProject(projectDir, projectName = "") {
        if (this._destroyed) return;

        this.projectName = projectName || "";
        this.state.projectName = projectName || "";
        this.syncStateFromWidgets();

        if (!projectDir) {
            if (this.fullscreenSession) {
                const session = this.fullscreenSession;
                this.fullscreenSession = null;
                session.destroy();
            }
            this.state.projectDir = "";
            this.state.dormantSummary = null;
            this._invalidateModules(["project", "assets", "scene", "queue"]);
            this.collapseModule();
            this.render();
            return;
        }

        if (projectDir !== this.state.projectDir) {
            if (this.fullscreenSession) {
                const session = this.fullscreenSession;
                this.fullscreenSession = null;
                session.destroy();
            }
            this.state.projectDir = projectDir;
            this.state.dormantSummary = null;
            this._invalidateModules(["project", "assets", "scene", "queue"]);
            this.state.expandedModuleId = "";
        }

        await this.refreshSummary();
        this.render();
    }

    async refreshSummary(options = {}) {
        const { syncAssets = false } = options;
        if (this._destroyed || !this.state.projectDir) return;

        if (this._summaryAborter) {
            this._summaryAborter.abort();
        }
        const aborter = new AbortController();
        this._summaryAborter = aborter;

        try {
            if (syncAssets) {
                const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets`), {
                    signal: aborter.signal,
                });
                if (!resp.ok) {
                    throw new Error(`Asset sync failed: ${resp.status}`);
                }
            }
            this.syncStateFromWidgets();
            this.state.dormantSummary = await fetchJson(
                buildDormantSummaryUrl(this.state),
                aborter.signal,
            );
        } catch (e) {
            if (e.name !== "AbortError") {
                console.warn("[LTX Editor] Failed to fetch dormant summary:", e);
            }
        } finally {
            if (this._summaryAborter === aborter) {
                this._summaryAborter = null;
            }
            this.render();
        }
    }

    toggleModule(moduleId) {
        if (!this.state.projectDir) return;
        if (this.state.expandedModuleId === moduleId) {
            this.collapseModule();
            return;
        }
        this.expandModule(moduleId);
    }

    expandModule(moduleId) {
        if (!this.modules[moduleId]) return;
        this._abortModuleLoad(this.state.expandedModuleId);
        this.state.expandedModuleId = moduleId;
        this.moduleStatus[moduleId].error = "";
        if (!this.moduleCache[moduleId]) {
            this._loadModule(moduleId);
        }
        this.render();
    }

    collapseModule() {
        this._abortModuleLoad(this.state.expandedModuleId);
        this.state.expandedModuleId = "";
        this.render();
    }

    _abortModuleLoad(moduleId) {
        if (!moduleId) return;
        const aborter = this._moduleLoadAborters[moduleId];
        if (aborter) {
            aborter.abort();
            delete this._moduleLoadAborters[moduleId];
        }
    }

    async _loadModule(moduleId) {
        if (this._destroyed || !moduleId || !this.modules[moduleId] || !this.state.projectDir) return;
        if (this._moduleLoadAborters[moduleId]) return;
        const moduleDef = this.modules[moduleId];
        const status = this.moduleStatus[moduleId];
        status.loading = true;
        status.error = "";
        const aborter = new AbortController();
        this._moduleLoadAborters[moduleId] = aborter;
        this.render();

        try {
            const data = await moduleDef.load(this, aborter.signal);
            if (aborter.signal.aborted) return;
            this.moduleCache[moduleId] = data;
        } catch (e) {
            if (e.name !== "AbortError") {
                status.error = e.message || "Failed to load";
            }
        } finally {
            if (this._moduleLoadAborters[moduleId] === aborter) {
                delete this._moduleLoadAborters[moduleId];
            }
            status.loading = false;
            this.render();
        }
    }

    _invalidateModules(keys) {
        for (const [moduleId, moduleDef] of Object.entries(this.modules)) {
            if (moduleDef.invalidate(keys)) {
                delete this.moduleCache[moduleId];
                this.moduleStatus[moduleId].error = "";
            }
        }
    }

    _reloadExpandedModuleIfNeeded(keys) {
        const expandedModuleId = this.state.expandedModuleId;
        if (!expandedModuleId) return;
        if (this.modules[expandedModuleId]?.invalidate(keys)) {
            this._loadModule(expandedModuleId);
        }
    }

    openFullscreen() {
        if (this._destroyed || !this.state.projectDir || this.fullscreenSession || this.state.isFullscreenOpen) return;

        this.syncStateFromWidgets();
        this.state.isFullscreenOpen = true;
        this.render();

        try {
            const session = new FullscreenEditorSession(this);
            this.fullscreenSession = session;
            session.mount();
        } catch (e) {
            console.warn("[LTX Editor] Failed to open fullscreen editor:", e);
            this.fullscreenSession = null;
            this.state.isFullscreenOpen = false;
            this.render();
        }
    }

    onFullscreenSessionDestroyed(session) {
        if (this._destroyed) return;
        if (this.fullscreenSession === session) {
            this.fullscreenSession = null;
        }

        this.state.isFullscreenOpen = false;
        this.syncStateFromWidgets();
        this._invalidateModules(["scene", "assets", "queue"]);
        this._reloadExpandedModuleIfNeeded(["scene", "assets", "queue"]);
        this.refreshSummary().finally(() => this.render());
    }

    handleNodeExecuted() {
        if (this._destroyed || !this.state.projectDir) return;
        this.syncStateFromWidgets();
        this._invalidateModules(["assets", "queue"]);
        this._reloadExpandedModuleIfNeeded(["assets", "queue"]);
        this.refreshSummary({ syncAssets: true }).finally(() => this.render());
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
    }

    handleSaveVideoExecuted() {
        if (this._destroyed || !this.state.projectDir) return;
        this._invalidateModules(["assets", "scene"]);
        this._reloadExpandedModuleIfNeeded(["assets", "scene"]);
        this.refreshSummary({ syncAssets: true }).finally(() => this.render());
        this.fullscreenSession?.refresh(["assets", "scenes"]);
    }

    async importFiles(files, folder = "") {
        if (this._destroyed || !this.state.projectDir || !files?.length) return false;

        let importedAny = false;
        for (const file of files) {
            if (await importFileIntoProject(this.state.projectDir, file, folder)) {
                importedAny = true;
            }
        }

        if (importedAny) {
            this._invalidateModules(["assets", "scene"]);
            this._reloadExpandedModuleIfNeeded(["assets", "scene"]);
            await this.refreshSummary({ syncAssets: true });
        }

        return importedAny;
    }

    async _updateAssetMetadata(assetId, updates) {
        if (!this.state.projectDir || !assetId) return null;

        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/${assetId}`), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(updates),
        });
        if (!resp.ok) {
            throw new Error(`Asset update failed: ${resp.status}`);
        }

        const updatedAsset = await resp.json();
        if (this.moduleCache.assets?.assets) {
            this.moduleCache.assets = {
                ...this.moduleCache.assets,
                assets: this.moduleCache.assets.assets.map((asset) => (
                    asset.asset_id === assetId
                        ? { ...asset, ...updatedAsset, has_thumbnail: asset.has_thumbnail }
                        : asset
                )),
            };
            if (updatedAsset.folder) {
                const folders = new Set(this.moduleCache.assets.folders || []);
                folders.add(updatedAsset.folder);
                this.moduleCache.assets.folders = Array.from(folders).sort((a, b) => a.localeCompare(b));
            }
        }
        this.fullscreenSession?.refresh(["assets"]);
        return updatedAsset;
    }

    async _getAssetUsages(assetId) {
        if (!this.state.projectDir || !assetId) return null;
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/${assetId}/usages`));
        if (!resp.ok) {
            throw new Error(`Asset usage fetch failed: ${resp.status}`);
        }
        return await resp.json();
    }

    async _deleteAsset(assetId, force = false) {
        if (!this.state.projectDir || !assetId) return { status: "noop" };

        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/${assetId}`), {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Asset delete failed: ${resp.status}`);
        }

        const payload = await resp.json();
        this._invalidateModules(["assets", "scene", "queue"]);
        this._reloadExpandedModuleIfNeeded(["assets", "scene", "queue"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        return { status: "deleted", ...(payload || {}) };
    }

    async _createAssetFolder(folderName) {
        if (!this.state.projectDir || !folderName) return [];
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/folders`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder: folderName }),
        });
        if (!resp.ok) {
            throw new Error(`Asset folder create failed: ${resp.status}`);
        }
        const payload = await resp.json();
        if (this.moduleCache.assets?.assets) {
            this.moduleCache.assets = {
                ...this.moduleCache.assets,
                folders: Array.isArray(payload.folders) ? payload.folders : (this.moduleCache.assets.folders || []),
            };
        }
        this.fullscreenSession?.refresh(["assets"]);
        return Array.isArray(payload.folders) ? payload.folders : [];
    }

    async _renameAssetFolder(folderName, newFolderName) {
        if (!this.state.projectDir || !folderName || !newFolderName) return [];
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/folders`), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ old_folder: folderName, new_folder: newFolderName }),
        });
        if (!resp.ok) {
            throw new Error(`Asset folder rename failed: ${resp.status}`);
        }
        const payload = await resp.json();
        this._invalidateModules(["assets"]);
        this._reloadExpandedModuleIfNeeded(["assets"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets"]);
        return payload || { folders: [] };
    }

    async _deleteAssetFolder(folderName, force = false) {
        if (!this.state.projectDir || !folderName) return { status: "noop" };
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/folders`), {
            method: "DELETE",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ folder: folderName, force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Asset folder delete failed: ${resp.status}`);
        }
        const payload = await resp.json();
        this._invalidateModules(["assets", "scene", "queue"]);
        this._reloadExpandedModuleIfNeeded(["assets", "scene", "queue"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        return { status: "deleted", ...(payload || {}) };
    }

    async _replaceAsset(assetId, file) {
        if (!this.state.projectDir || !assetId || !file) return null;
        const payload = await replaceAssetInProject(this.state.projectDir, assetId, file);
        this._invalidateModules(["assets", "scene", "queue"]);
        this._reloadExpandedModuleIfNeeded(["assets", "scene", "queue"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        return payload?.asset || null;
    }

    async _loadDormantAssets(signal) {
        const payload = await fetchJson(buildDormantAssetsUrl(this.state.projectDir), signal);
        return Array.isArray(payload)
            ? { assets: payload, folders: [] }
            : { assets: payload.assets || [], folders: payload.folders || [] };
    }

    async _loadQueueModule(signal) {
        const queue = await fetchJson(buildQueueUrl(this.state.projectDir), signal);
        return Array.isArray(queue) ? queue : [];
    }

    async _loadPreviewModule(signal) {
        const summary = this.state.dormantSummary;
        const sceneId = summary?.active_scene?.scene_id || this.state.sceneId;
        if (!sceneId) {
            return {
                kind: "empty",
                label: "No scene",
                subtitle: "Open the editor to choose a scene.",
            };
        }

        const assetsPromise = this.moduleCache.assets?.assets
            ? Promise.resolve(this.moduleCache.assets.assets)
            : this._loadDormantAssets(signal).then((payload) => {
                this.moduleCache.assets = payload;
                return payload.assets;
            });

        const [scene, assets] = await Promise.all([
            fetchJson(buildSceneUrl(this.state.projectDir, sceneId), signal),
            assetsPromise,
        ]);

        return pickPreviewTarget(this.state.projectDir, summary, scene, assets);
    }

    _mountAssetsModule(container, data) {
        const galleryHost = container.appendChild(style(document.createElement("div"), `
            display: flex;
            flex-direction: column;
            flex: 1 1 auto;
            min-height: 0;
            height: 100%;
            width: 100%;
            overflow: hidden;
        `));
        const gallery = mountSharedAssetGallery(galleryHost, {
            getProjectDir: () => this.state.projectDir,
            initialData: data || { assets: [], folders: [] },
            onImportFiles: async (files, folder) => await this.importFiles(files, folder),
            onUpdateAsset: async (assetId, updates) => await this._updateAssetMetadata(assetId, updates),
            onGetAssetUsages: async (assetId) => await this._getAssetUsages(assetId),
            onDeleteAsset: async (assetId, force) => await this._deleteAsset(assetId, force),
            onCreateFolder: async (folderName) => await this._createAssetFolder(folderName),
            onRenameFolder: async (folderName, newFolderName) => await this._renameAssetFolder(folderName, newFolderName),
            onDeleteFolder: async (folderName, force) => await this._deleteAssetFolder(folderName, force),
            onReplaceAsset: async (assetId, file) => await this._replaceAsset(assetId, file),
            onRefresh: async () => {
                const payload = await this._loadDormantAssets();
                this.moduleCache.assets = payload;
                gallery.setData(payload);
                this.card.syncModuleContainerHeight?.();
            },
            onRequestResize: () => this.card.syncModuleContainerHeight?.(),
        });
        return () => gallery.destroy();
    }

    _mountPreviewModule(container, data) {
        const wrap = style(document.createElement("div"), `
            display: flex;
            flex-direction: column;
            gap: 8px;
        `);
        container.appendChild(wrap);

        const header = style(document.createElement("div"), `
            display: flex;
            flex-direction: column;
            gap: 2px;
        `);
        const title = style(document.createElement("div"), `
            color: #ececec;
            font-size: 11px;
            font-weight: 600;
        `);
        title.textContent = data.label || "Preview";
        const subtitle = style(document.createElement("div"), `
            color: #8ea0af;
            font-size: 10px;
        `);
        subtitle.textContent = data.subtitle || "";
        header.append(title, subtitle);
        wrap.appendChild(header);

        const surface = style(document.createElement("div"), `
            min-height: 110px;
            border-radius: 6px;
            border: 1px solid #373737;
            background: #111;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        `);
        wrap.appendChild(surface);

        let liveVideo = null;

        const destroyVideo = () => {
            if (!liveVideo) return;
            liveVideo.pause();
            liveVideo.removeAttribute("src");
            liveVideo.load();
            liveVideo.remove();
            liveVideo = null;
        };

        if (data.kind === "empty" || data.kind === "missing") {
            const emptyEl = style(document.createElement("div"), `
                color: ${data.kind === "missing" ? "#ffb18c" : "#8ea0af"};
                font-size: 10px;
                padding: 14px;
                text-align: center;
            `);
            emptyEl.textContent = data.kind === "missing"
                ? `Missing asset: ${data.subtitle || "Select a replacement from the gallery."}`
                : (data.subtitle || "No preview available.");
            surface.appendChild(emptyEl);
        } else if (data.kind === "image") {
            const img = style(document.createElement("img"), `
                width: 100%;
                height: 100%;
                max-height: 180px;
                object-fit: cover;
                display: block;
            `);
            img.src = data.posterUrl;
            img.alt = data.subtitle || "Preview";
            surface.appendChild(img);
        } else if (data.kind === "video") {
            if (data.posterUrl) {
                const poster = style(document.createElement("img"), `
                    width: 100%;
                    height: 100%;
                    max-height: 180px;
                    object-fit: cover;
                    display: block;
                `);
                poster.src = data.posterUrl;
                poster.alt = data.subtitle || "Preview";
                surface.appendChild(poster);
            } else {
                const placeholder = style(document.createElement("div"), `
                    color: #8ea0af;
                    font-size: 10px;
                `);
                placeholder.textContent = "Preview is available on demand.";
                surface.appendChild(placeholder);
            }

            const loadBtn = style(document.createElement("button"), `
                position: absolute;
                bottom: 8px;
                right: 8px;
                padding: 5px 8px;
                border-radius: 6px;
                border: 1px solid #5f7d97;
                background: rgba(32,48,62,0.92);
                color: #fff;
                cursor: pointer;
                font-size: 10px;
                font-weight: 600;
            `);
            loadBtn.textContent = "Load Preview";
            loadBtn.addEventListener("click", () => {
                if (liveVideo) return;
                surface.innerHTML = "";
                liveVideo = style(document.createElement("video"), `
                    width: 100%;
                    max-height: 180px;
                    display: block;
                    background: #000;
                `);
                liveVideo.controls = true;
                liveVideo.preload = "metadata";
                liveVideo.src = data.mediaUrl;
                surface.appendChild(liveVideo);
            });
            surface.appendChild(loadBtn);
        }

        return () => {
            destroyVideo();
        };
    }

    _mountQueueModule(container, data) {
        const wrap = style(document.createElement("div"), `
            display: flex;
            flex-direction: column;
            gap: 6px;
            max-height: 220px;
            overflow-y: auto;
            padding-right: 2px;
        `);
        container.appendChild(wrap);

        const jobs = Array.isArray(data) ? data.slice(0, 8) : [];
        if (!jobs.length) {
            const emptyEl = style(document.createElement("div"), `
                padding: 10px;
                border-radius: 6px;
                background: rgba(255,255,255,0.04);
                color: #9aa6b2;
            `);
            emptyEl.textContent = "Render queue is empty.";
            wrap.appendChild(emptyEl);
            return null;
        }

        const colors = {
            pending: "#888",
            running: "#4a9eff",
            completed: "#4a4",
            failed: "#c44",
        };

        for (const job of jobs) {
            const row = style(document.createElement("div"), `
                display: grid;
                grid-template-columns: auto 1fr;
                gap: 8px;
                padding: 7px 8px;
                border-radius: 6px;
                background: rgba(255,255,255,0.03);
                border: 1px solid #343434;
                align-items: start;
            `);

            const dot = style(document.createElement("span"), `
                width: 8px;
                height: 8px;
                margin-top: 4px;
                border-radius: 50%;
                background: ${colors[job.status] || "#888"};
            `);

            const text = style(document.createElement("div"), `
                min-width: 0;
                display: flex;
                flex-direction: column;
                gap: 2px;
            `);
            const title = style(document.createElement("div"), `
                color: #ececec;
                font-size: 11px;
                font-weight: 600;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            `);
            title.textContent = job.scene_name || "Scene";

            const meta = style(document.createElement("div"), `
                color: #8ea0af;
                font-size: 10px;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            `);
            meta.textContent = `${job.status || "pending"} | ${formatFrameRange(job.selection_start, job.selection_end)}`;

            text.append(title, meta);
            row.append(dot, text);
            wrap.appendChild(row);
        }

        return null;
    }
}
