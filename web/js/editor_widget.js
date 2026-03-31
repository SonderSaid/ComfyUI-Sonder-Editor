/**
 * LTX Editor Widget — Timeline + Asset Gallery embedded in a ComfyUI node.
 * Uses addDOMWidget pattern (same as VHS/KJNodes).
 */

const { api } = window.comfyAPI.api;

// ── Constants ──────────────────────────────────────────────────────────
const TIMELINE_HEIGHT = 212;
const GALLERY_HEIGHT = 160;
const RULER_HEIGHT = 24;
const TRACK_HEIGHT = 32;
const SCENE_BAR_HEIGHT = 36;
const TRACK_TYPE = { VIDEO: "video", AUDIO: "audio", GUIDES: "guides", PROMPT: "prompt" };
const TRACK_COLLAPSED_HEIGHT = 8; // Height when a track is collapsed
const LABEL_WIDTH = 55; // px reserved for track labels (node mode)
const LABEL_WIDTH_FS = 70; // px reserved for track labels (fullscreen)
const LANE_PALETTE = [
    "#3a6ea5",  // Blue
    "#6a8e3e",  // Green
    "#a05a2c",  // Orange
    "#7e4a8a",  // Purple
    "#8a6b3e",  // Gold
    "#3a8a7a",  // Teal
];
const COLORS = {
    bg: "#1a1a1a",
    ruler: "#2a2a2a",
    rulerText: "#888",
    rulerTick: "#555",
    track: "#252525",
    trackBorder: "#333",
    clip: "#3a7ca5",
    clipSelected: "#5aacD5",
    audioClip: "#5a8a5a",
    audioClipSelected: "#7aba7a",
    guide: "#e8a030",
    guideSelected: "#ffcc44",
    selection: "rgba(100, 180, 255, 0.15)",
    selectionBorder: "rgba(100, 180, 255, 0.6)",
    playhead: "#ff4444",
    promptSection: "rgba(180, 120, 255, 0.2)",
    promptBorder: "rgba(180, 120, 255, 0.5)",
    galleryBg: "#1e1e1e",
    galleryItem: "#2a2a2a",
    galleryItemHover: "#3a3a3a",
    galleryItemBorder: "#444",
    galleryText: "#ccc",
    galleryLabel: "#888",
    sceneBar: "#222",
    sceneBtn: "#333",
    sceneBtnHover: "#444",
    sceneBtnActive: "#3a7ca5",
    text: "#ddd",
    textDim: "#777",
};

// ── Editor Widget Class ────────────────────────────────────────────────
export class EditorWidget {
    constructor(node) {
        this.node = node;
        this.projectDir = "";
        this.projectId = "";

        // Scene state
        this.scenes = [];
        this.activeSceneId = "";
        this.activeScene = null;

        // Timeline state
        this.totalFrames = 200;
        this.selectionStart = 0;
        this.selectionEnd = 0;
        this.playhead = 0;
        this.scrollX = 0;
        this.pixelsPerFrame = 3;
        this.isDragging = false;
        this.dragType = null; // "selection", "playhead", "selStart", "selEnd"

        // Asset state
        this.assets = { video: [], image: [], audio: [] };
        this.selectedAssetType = "video";

        // Prompt section state
        this._selectedPromptIdx = null;

        // Selected timeline items: array of { type: "clip"|"guide"|"audio", id: string|number, data: object }
        this.selectedItems = [];
        this.selectedItem = null; // Primary (most recently clicked) — used for properties editor

        // Razor / trim mode
        this._razorMode = false;
        this._trimItem = null;   // { type, id, data, edge: "left"|"right", origStart, origEnd, origSourceIn, origSourceOut }

        // Drag-to-move state
        this._dragStartFrame = 0;
        this._dragItemOrigStart = 0;
        this._dragItemOrigEnd = 0;

        // Context menu
        this._contextMenuEl = null;

        // Project settings (fetched from server)
        this.fps = 24;
        this.sceneWidth = 768;
        this.sceneHeight = 512;

        // Viewport / playback state
        this.isPlaying = false;
        this._playbackRAF = null;
        this._playbackStartTime = 0;
        this._playbackStartFrame = 0;
        this._videoCache = {};       // source_path → HTMLVideoElement
        this._audioCacheMap = {};    // source_path → HTMLAudioElement
        this._vpCanvas = null;
        this._vpCtx = null;
        this._vpSeekDebounce = null;
        this._activePlaybackVideo = null;
        this._activePlaybackAudios = [];

        // Snapping
        this.snappingEnabled = true;
        this._snapThreshold = 5; // frames
        this._snapIndicator = null; // frame number of active snap line, or null

        // Track layout: dynamically built array of { type, label, laneIndex, collapsed }
        this._trackLayout = [];
        this._buildTrackLayout();

        // Timecode display mode: "frames" or "timecode" (HH:MM:SS:FF)
        this._timecodeMode = "frames";

        // Undo/Redo state
        this._undoStack = [];   // Array of { sceneId, snapshot, label }
        this._redoStack = [];
        this._maxUndoSteps = 50;

        // Thumbnail strip cache: { assetId: { img: Image, frameWidth, numFrames, loaded } }
        this._thumbStripCache = {};
        // Waveform cache: { assetId: { peaks: [[min,max],...], numBuckets, loaded } }
        this._waveformCache = {};
        // Asset path→assetId reverse lookup (rebuilt on asset fetch)
        this._pathToAsset = {};

        // UI scale factor (persisted via localStorage)
        this._uiScale = parseFloat(localStorage.getItem("ltx-editor-ui-scale") || "1.0");

        // Editor focus state — true when user last clicked inside the editor
        this._editorFocused = false;

        // Fullscreen state
        this.isFullscreen = false;
        this._timelineHeight = TIMELINE_HEIGHT;
        this._galleryHeight = GALLERY_HEIGHT;
        this._fullscreenOverlay = null;
        this._fullscreenPlaceholder = null;
        this._nodeParent = null;
        this._nodeSibling = null;

        // Build DOM
        this._buildDOM();
        this._setupKeyboardEvents();

        // Apply initial UI scale (bars + canvas will be scaled on first render)
        if (this._uiScale !== 1.0) this._applyUiScale();

        // Window resize handler for fullscreen
        window.addEventListener("resize", () => {
            if (this.isFullscreen) {
                this._recalcFullscreenHeights();
                this._renderTimeline();
            }
        });

        // ResizeObserver on container to auto-re-render timeline when size changes
        this._containerResizeObserver = new ResizeObserver(() => {
            this._renderTimeline();
        });
    }

    _buildDOM() {
        // Main container
        this.container = document.createElement("div");
        this.container.dataset.ltxEditor = "1"; // marker for global drop interceptor
        this.container.style.cssText = `
            width: 100%; display: flex; flex-direction: column;
            padding: 4px 8px;
            font-family: 'Segoe UI', Arial, sans-serif; font-size: 11px;
            color: ${COLORS.text}; user-select: none;
            box-sizing: border-box;
        `;

        // Scene bar
        this._buildSceneBar();

        // Toolbar (snap, cut, split, shortcut hints)
        this._buildToolbar();

        // Timeline canvas
        this.timelineCanvas = document.createElement("canvas");
        this.timelineCanvas.style.cssText = `width: 100%; cursor: crosshair;`;
        this.timelineCanvas.height = this._timelineHeight;
        this.container.appendChild(this.timelineCanvas);

        // Timeline info bar
        this._buildInfoBar();

        // Asset gallery
        this._buildAssetGallery();

        // Events
        this._setupTimelineEvents();

        // Observe container size changes for auto-resize
        if (this._containerResizeObserver) {
            this._containerResizeObserver.observe(this.container);
        }
    }

    _buildSceneBar() {
        const bar = document.createElement("div");
        bar.style.cssText = `
            display: flex; align-items: center; gap: 4px;
            padding: 4px 6px; background: ${COLORS.sceneBar};
            border-bottom: 1px solid #333; min-height: ${SCENE_BAR_HEIGHT}px;
        `;

        // Prev scene
        const prevBtn = this._makeBtn("◀", "Previous scene");
        prevBtn.addEventListener("click", () => this._cycleScene(-1));

        // Scene label
        this.sceneLabel = document.createElement("span");
        this.sceneLabel.style.cssText = `
            flex: 1; text-align: center; font-size: 12px; font-weight: 600;
            color: ${COLORS.text}; overflow: hidden; text-overflow: ellipsis;
            white-space: nowrap; cursor: pointer;
        `;
        this.sceneLabel.textContent = "No Scene";
        this.sceneLabel.title = "Double-click to rename · Right-click for options";
        this.sceneLabel.addEventListener("dblclick", () => this._renameScene());
        this.sceneLabel.addEventListener("contextmenu", (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (!this.activeScene) return;

            const menuItems = [
                { label: "Rename Scene", action: () => this._renameScene() },
                { label: "Duplicate Scene", action: () => this._duplicateScene() },
                { label: "Delete Scene", action: () => this._deleteScene(), danger: true },
            ];
            this._showContextMenu(e.clientX, e.clientY, menuItems);
        });

        // Next scene
        const nextBtn = this._makeBtn("▶", "Next scene");
        nextBtn.addEventListener("click", () => this._cycleScene(1));

        // Add scene
        const addBtn = this._makeBtn("+", "Create new scene");
        addBtn.style.color = "#8c8";
        addBtn.addEventListener("click", () => this._createScene());

        // Duration input
        const durLabel = document.createElement("span");
        durLabel.style.cssText = `color: ${COLORS.textDim}; font-size: 10px; margin-left: 8px;`;
        durLabel.textContent = "Frames:";

        this.durationInput = document.createElement("input");
        this.durationInput.type = "number";
        this.durationInput.min = 1;
        this.durationInput.max = 99999;
        this.durationInput.value = 200;
        this.durationInput.style.cssText = `
            width: 55px; background: #333; border: 1px solid #555; color: #ddd;
            padding: 2px 4px; font-size: 11px; border-radius: 3px; text-align: center;
        `;
        this.durationInput.addEventListener("change", () => {
            this.totalFrames = Math.max(1, parseInt(this.durationInput.value) || 200);
            if (this.activeScene) {
                this._updateSceneDuration(this.totalFrames);
            }
            this._renderTimeline();
        });

        bar.append(prevBtn, this.sceneLabel, nextBtn, addBtn, durLabel, this.durationInput);
        this._sceneBar = bar;
        this.container.appendChild(bar);
    }

    _buildToolbar() {
        this._toolbar = document.createElement("div");
        this._toolbar.style.cssText = `
            display: flex; align-items: center; gap: 2px;
            padding: 2px 6px; background: #1e1e1e;
            border-bottom: 1px solid #333; font-size: 10px; min-height: 24px;
        `;

        // Tool buttons with active states
        const makeToolBtn = (label, shortcut, tooltip, getter, toggle) => {
            const btn = document.createElement("button");
            btn.style.cssText = `
                background: #333; border: 1px solid #555; color: #ccc;
                padding: 2px 8px; font-size: 10px; border-radius: 3px;
                cursor: pointer; white-space: nowrap;
            `;
            btn.textContent = label;
            btn.title = `${tooltip} [${shortcut}]`;
            btn.addEventListener("click", toggle);
            return btn;
        };

        this._toolBtnSnap = makeToolBtn("⊞ Snap", "S", "Toggle snapping", () => this.snappingEnabled, () => {
            this.snappingEnabled = !this.snappingEnabled;
            this._updateToolbar();
        });

        this._toolBtnRazor = makeToolBtn("✂ Cut", "C", "Toggle razor/cut mode", () => this._razorMode, () => {
            this._razorMode = !this._razorMode;
            this._updateToolbar();
        });

        const cutHereBtn = makeToolBtn("⌇ Split Here", "", "Split clip/audio at playhead", () => false, () => {
            // Find clip or audio at playhead and split
            const clip = this._getClipAtFrame(this.playhead);
            if (clip) {
                const hit = { type: "clip", id: clip.clip_id, data: clip };
                this._splitClipAtFrame(hit, this.playhead);
            }
            const audio = this._getAudioAtFrame(this.playhead);
            if (audio) {
                const hit = { type: "audio", id: audio.track_id, data: audio };
                this._splitClipAtFrame(hit, this.playhead);
            }
        });
        cutHereBtn.title = "Split clip/audio at current playhead position";

        // Separator
        const sep1 = document.createElement("span");
        sep1.style.cssText = `width: 1px; height: 16px; background: #444; margin: 0 4px;`;

        // Selection (In/Out) display
        this._selectionLabel = document.createElement("span");
        this._selectionLabel.style.cssText = `color: #999; font-size: 9px; white-space: nowrap; min-width: 80px;`;
        this._selectionLabel.textContent = "In/Out: —";

        const clearSelBtn = makeToolBtn("✕", "X", "Clear selection", () => false, () => {
            this.selectionStart = 0;
            this.selectionEnd = 0;
            this._setWidgetValue("selection_start", 0);
            this._setWidgetValue("selection_end", 0);
            this._renderTimeline();
            this._updateInfoLabel();
            this._updateToolbar();
        });
        clearSelBtn.style.padding = "2px 4px";
        clearSelBtn.style.fontSize = "9px";

        // Separator 2
        const sep2 = document.createElement("span");
        sep2.style.cssText = `width: 1px; height: 16px; background: #444; margin: 0 4px;`;

        // Fit to view button
        const fitBtn = makeToolBtn("⊞ Fit", "F", "Fit timeline to view", () => false, () => {
            this._fitToView();
        });

        // Timecode toggle button
        this._toolBtnTimecode = makeToolBtn("TC", "T", "Toggle timecode/frame display", () => this._timecodeMode === "timecode", () => {
            this._toggleTimecodeMode();
        });

        // Undo/Redo buttons
        const undoBtn = this._makeBtn("↩", "Undo (Ctrl+Z)");
        undoBtn.style.fontSize = "13px";
        undoBtn.addEventListener("click", () => this._undo());

        const redoBtn = this._makeBtn("↪", "Redo (Ctrl+Y)");
        redoBtn.style.fontSize = "13px";
        redoBtn.addEventListener("click", () => this._redo());

        // Separator 3
        const sep3 = document.createElement("span");
        sep3.style.cssText = `width: 1px; height: 16px; background: #444; margin: 0 4px;`;

        // Spacer
        const spacer = document.createElement("span");
        spacer.style.flex = "1";

        // Shortcut help button
        const helpBtn = document.createElement("button");
        helpBtn.textContent = "?";
        helpBtn.title = "Keyboard Shortcuts";
        helpBtn.style.cssText = `
            background: #333; border: 1px solid #555; color: #aaa; cursor: pointer;
            padding: 1px 7px; border-radius: 10px; font-size: 10px; font-weight: bold;
        `;
        helpBtn.addEventListener("click", () => this._showShortcutOverlay());

        this._toolbar.append(undoBtn, redoBtn, sep3, this._toolBtnSnap, this._toolBtnRazor, cutHereBtn, sep1, this._selectionLabel, clearSelBtn, sep2, fitBtn, this._toolBtnTimecode, spacer, helpBtn);
        this.container.appendChild(this._toolbar);
        this._updateToolbar();
    }

    _updateToolbar() {
        if (!this._toolBtnSnap) return;
        this._toolBtnSnap.style.background = this.snappingEnabled ? "#3a7ca5" : "#333";
        this._toolBtnSnap.style.color = this.snappingEnabled ? "#fff" : "#ccc";
        this._toolBtnRazor.style.background = this._razorMode ? "#a53a3a" : "#333";
        this._toolBtnRazor.style.color = this._razorMode ? "#fff" : "#ccc";
        if (this._toolBtnTimecode) {
            const isTc = this._timecodeMode === "timecode";
            this._toolBtnTimecode.style.background = isTc ? "#5a5a3a" : "#333";
            this._toolBtnTimecode.style.color = isTc ? "#ffd" : "#ccc";
        }

        // Update selection display
        if (this._selectionLabel) {
            if (this.selectionStart < this.selectionEnd) {
                const dur = this.selectionEnd - this.selectionStart;
                this._selectionLabel.textContent = `In: ${this._frameToTimecode(this.selectionStart)} Out: ${this._frameToTimecode(this.selectionEnd)} (${this._frameToTimecode(dur)})`;
                this._selectionLabel.style.color = "#8cf";
            } else {
                this._selectionLabel.textContent = "In/Out: —";
                this._selectionLabel.style.color = "#999";
            }
        }
    }

    _buildInfoBar() {
        const bar = document.createElement("div");
        bar.style.cssText = `
            display: flex; align-items: center; justify-content: space-between;
            padding: 3px 6px; background: ${COLORS.sceneBar};
            border-top: 1px solid #333; font-size: 10px; color: ${COLORS.textDim};
        `;

        this.infoLabel = document.createElement("span");
        this.infoLabel.textContent = "Selection: none";

        const zoomContainer = document.createElement("span");
        zoomContainer.style.display = "flex";
        zoomContainer.style.gap = "4px";
        zoomContainer.style.alignItems = "center";

        const zoomOut = this._makeBtn("−", "Zoom out");
        zoomOut.style.fontSize = "13px";
        zoomOut.addEventListener("click", () => this._zoom(-1));

        const zoomIn = this._makeBtn("+", "Zoom in");
        zoomIn.style.fontSize = "13px";
        zoomIn.addEventListener("click", () => this._zoom(1));

        this._fullscreenBtn = this._makeBtn("⛶", "Toggle fullscreen");
        this._fullscreenBtn.style.fontSize = "14px";
        this._fullscreenBtn.addEventListener("click", () => this._toggleFullscreen());

        // UI Scale controls
        const scaleContainer = document.createElement("span");
        scaleContainer.style.cssText = "display:flex; gap:2px; align-items:center; margin-right:8px;";
        const scaleDn = this._makeBtn("A−", "Decrease UI scale");
        scaleDn.style.fontSize = "9px";
        scaleDn.addEventListener("click", () => this._setUiScale(this._uiScale - 0.1));
        this._scaleLabel = document.createElement("span");
        this._scaleLabel.style.cssText = "font-size:9px; color:#888; min-width:28px; text-align:center;";
        this._scaleLabel.textContent = `${Math.round(this._uiScale * 100)}%`;
        const scaleUp = this._makeBtn("A+", "Increase UI scale");
        scaleUp.style.fontSize = "9px";
        scaleUp.addEventListener("click", () => this._setUiScale(this._uiScale + 0.1));
        scaleContainer.append(scaleDn, this._scaleLabel, scaleUp);

        zoomContainer.append(this._fullscreenBtn, scaleContainer, zoomOut, zoomIn);
        bar.append(this.infoLabel, zoomContainer);
        this._infoBar = bar;
        this.container.appendChild(bar);
    }

    _buildAssetGallery() {
        const gallery = document.createElement("div");
        gallery.style.cssText = `
            background: ${COLORS.galleryBg}; border-top: 1px solid #333;
            min-height: ${this._galleryHeight}px; overflow: hidden;
        `;

        // Tab bar
        const tabs = document.createElement("div");
        tabs.style.cssText = `
            display: flex; border-bottom: 1px solid #333;
        `;

        this.tabBtns = {};
        for (const type of ["video", "image", "audio"]) {
            const tab = document.createElement("button");
            tab.textContent = type.charAt(0).toUpperCase() + type.slice(1) + "s";
            tab.style.cssText = `
                flex: 1; padding: 5px 8px; background: none; border: none;
                color: ${COLORS.galleryLabel}; font-size: 11px; cursor: pointer;
                border-bottom: 2px solid transparent; transition: all 0.15s;
            `;
            tab.addEventListener("click", () => this._selectAssetTab(type));
            tabs.appendChild(tab);
            this.tabBtns[type] = tab;
        }

        // Refresh button
        const refreshBtn = document.createElement("button");
        refreshBtn.textContent = "Refresh";
        refreshBtn.title = "Refresh assets (R)";
        refreshBtn.style.cssText = `
            padding: 4px 8px; background: none; border: none;
            color: ${COLORS.textDim}; font-size: 10px; cursor: pointer;
        `;
        refreshBtn.addEventListener("click", () => this._fetchAssets());
        refreshBtn.addEventListener("mouseenter", () => refreshBtn.style.color = COLORS.text);
        refreshBtn.addEventListener("mouseleave", () => refreshBtn.style.color = COLORS.textDim);
        tabs.appendChild(refreshBtn);

        // Asset grid
        this.assetGrid = document.createElement("div");
        this.assetGrid.style.cssText = `
            display: flex; flex-wrap: wrap; gap: 6px; padding: 6px;
            overflow-y: auto; max-height: ${this._galleryHeight - 30}px;
            min-height: 60px;
        `;

        // Empty message
        this.emptyMsg = document.createElement("div");
        this.emptyMsg.style.cssText = `
            width: 100%; text-align: center; padding: 20px;
            color: ${COLORS.textDim}; font-size: 11px;
        `;
        this.emptyMsg.textContent = "No assets yet. Import media into your project.";
        this.assetGrid.appendChild(this.emptyMsg);

        gallery.append(tabs, this.assetGrid);
        this.container.appendChild(gallery);
        this.galleryEl = gallery;

        // External file drop zone on gallery
        gallery.addEventListener("dragover", (e) => {
            // Only handle file drops (not internal asset drags)
            if (e.dataTransfer.types.includes("Files")) {
                e.preventDefault();
                e.stopPropagation();
                e.dataTransfer.dropEffect = "copy";
                gallery.style.outline = "2px dashed " + COLORS.selectionBorder;
            }
        });
        gallery.addEventListener("dragleave", () => {
            gallery.style.outline = "none";
        });
        gallery.addEventListener("drop", (e) => {
            gallery.style.outline = "none";
            if (e.dataTransfer.files?.length > 0) {
                e.preventDefault();
                e.stopPropagation();
                for (const file of e.dataTransfer.files) {
                    this._importFile(file);
                }
            }
        });

        // Default to video tab
        this._selectAssetTab("video");
    }

    // ── Button Helper ──────────────────────────────────────────────────
    _makeBtn(text, title = "") {
        const btn = document.createElement("button");
        btn.textContent = text;
        btn.title = title;
        btn.style.cssText = `
            background: ${COLORS.sceneBtn}; border: 1px solid #555;
            color: ${COLORS.text}; padding: 2px 8px; cursor: pointer;
            border-radius: 3px; font-size: 12px; line-height: 1.4;
        `;
        btn.addEventListener("mouseenter", () => btn.style.background = COLORS.sceneBtnHover);
        btn.addEventListener("mouseleave", () => btn.style.background = COLORS.sceneBtn);
        return btn;
    }

    // ── Scene Management ───────────────────────────────────────────────
    async _fetchScenes() {
        if (!this.projectDir) return;

        try {
            const dirName = this.projectDir.split(/[/\\]/).pop();
            const resp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes`));
            if (resp.ok) {
                const data = await resp.json();
                this.scenes = data.scenes || [];
                if (this.scenes.length > 0 && !this.activeSceneId) {
                    this._setActiveScene(this.scenes[0]);
                } else if (this.activeSceneId) {
                    const scene = this.scenes.find(s => s.scene_id === this.activeSceneId);
                    if (scene) this._setActiveScene(scene);
                }
            }
        } catch (e) {
            console.warn("[LTX Editor] Failed to fetch scenes:", e);
        }
    }

    async _createScene() {
        if (!this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            const resp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name: `Scene ${this.scenes.length + 1}`,
                    duration_frames: parseInt(this.durationInput.value) || 200,
                }),
            });
            if (resp.ok) {
                const scene = await resp.json();
                this.scenes.push(scene);
                this._setActiveScene(scene);
            }
        } catch (e) {
            console.warn("[LTX Editor] Failed to create scene:", e);
        }
    }

    _setActiveScene(scene) {
        const isSameScene = this.activeSceneId === scene.scene_id;

        if (!isSameScene) {
            this._stopPlayback();
            // Clear undo/redo on scene switch (snapshots are scene-specific)
            this._undoStack = [];
            this._redoStack = [];
            this.selectionStart = 0;
            this.selectionEnd = 0;
            this.playhead = 0;
            this._selectedPromptIdx = null;
            this._hidePromptEditor();
            this._clearSelection();
            this._hideItemEditor();
            this._setWidgetValue("selection_start", 0);
            this._setWidgetValue("selection_end", 0);
        }

        this.activeScene = scene;
        this.activeSceneId = scene.scene_id;
        this._buildTrackLayout();
        this.totalFrames = scene.duration_frames || 200;
        this.durationInput.value = this.totalFrames;
        this.sceneLabel.textContent = scene.name || "Untitled Scene";

        // Update hidden widgets
        this._setWidgetValue("scene_id", scene.scene_id);

        // Update fullscreen title if in fullscreen
        if (this.isFullscreen && this._fsTitle) {
            this._fsTitle.textContent = `Editor — ${scene.name || "No Scene"}`;
        }

        if (!isSameScene) {
            // Defer auto-fit to next frame so browser reflows after editor hide
            this._renderTimeline(); // Immediate render with new scene data
            requestAnimationFrame(() => this._fitToView());
        } else {
            this._renderTimeline();
        }
        this._renderViewportFrame();
    }

    _cycleScene(dir) {
        if (this.scenes.length === 0) return;
        const idx = this.scenes.findIndex(s => s.scene_id === this.activeSceneId);
        let newIdx = idx + dir;
        if (newIdx < 0) newIdx = this.scenes.length - 1;
        if (newIdx >= this.scenes.length) newIdx = 0;
        this._setActiveScene(this.scenes[newIdx]);
    }

    async _renameScene() {
        if (!this.activeScene) return;
        const name = prompt("Scene name:", this.activeScene.name);
        if (!name || name === this.activeScene.name) return;
        this._pushUndo("rename scene");

        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
            this.activeScene.name = name;
            this.sceneLabel.textContent = name;
        } catch (e) {
            console.warn("[LTX Editor] Failed to rename scene:", e);
        }
    }

    async _updateSceneDuration(frames) {
        if (!this.activeScene || !this.projectDir) return;
        this._pushUndo("change duration");
        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ duration_frames: frames }),
            });
            this.activeScene.duration_frames = frames;
        } catch (e) {
            console.warn("[LTX Editor] Failed to update scene duration:", e);
        }
    }

    async _deleteScene() {
        if (!this.activeScene || !this.projectDir) return;
        if (this.scenes.length <= 1) {
            alert("Cannot delete the last scene.");
            return;
        }
        if (!confirm(`Delete scene "${this.activeScene.name}"? This cannot be undone.`)) return;

        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "DELETE",
            });
            await this._fetchScenes();
        } catch (e) {
            console.warn("[LTX Editor] Failed to delete scene:", e);
        }
    }

    async _duplicateScene() {
        if (!this.activeScene || !this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            // Create new scene with same duration
            const resp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    name: `${this.activeScene.name} (copy)`,
                    duration_frames: this.activeScene.duration_frames || 200,
                }),
            });

            if (!resp.ok) return;
            const newScene = await resp.json();
            const newId = newScene.scene_id;

            // Copy guide frames
            for (const guide of (this.activeScene.guide_frames || [])) {
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${newId}/guides`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        frame_index: guide.frame_index,
                        asset_id: guide.asset_id,
                        source: guide.source || "asset",
                        strength: guide.strength ?? 1.0,
                    }),
                });
            }

            // Copy prompt sections
            for (const section of (this.activeScene.prompt_sections || [])) {
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${newId}/prompt_sections`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        start_frame: section.start_frame,
                        end_frame: section.end_frame,
                        prompt: section.prompt,
                    }),
                });
            }

            await this._fetchScenes();
            // Switch to the new scene
            const copied = this.scenes.find(s => s.scene_id === newId);
            if (copied) this._setActiveScene(copied);
        } catch (e) {
            console.warn("[LTX Editor] Failed to duplicate scene:", e);
        }
    }

    // ── Asset Management ───────────────────────────────────────────────
    async _fetchAssets() {
        if (!this.projectDir) return;

        try {
            const dirName = this.projectDir.split(/[/\\]/).pop();
            const resp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}/assets`));
            if (resp.ok) {
                const data = await resp.json();
                this.assets = { video: [], image: [], audio: [] };
                this._pathToAsset = {};
                for (const asset of (data.assets || [])) {
                    if (this.assets[asset.asset_type]) {
                        this.assets[asset.asset_type].push(asset);
                    }
                    if (asset.path) this._pathToAsset[asset.path] = asset;
                }
                this._renderAssetGrid();
            }
        } catch (e) {
            console.warn("[LTX Editor] Failed to fetch assets:", e);
        }
    }

    _selectAssetTab(type) {
        this.selectedAssetType = type;
        for (const [t, btn] of Object.entries(this.tabBtns)) {
            const count = (this.assets[t] || []).length;
            const label = t.charAt(0).toUpperCase() + t.slice(1) + "s";
            btn.textContent = count > 0 ? `${label} (${count})` : label;
            btn.style.color = t === type ? COLORS.text : COLORS.galleryLabel;
            btn.style.borderBottomColor = t === type ? COLORS.sceneBtnActive : "transparent";
        }
        this._renderAssetGrid();
    }

    _renderAssetGrid() {
        this.assetGrid.innerHTML = "";
        const items = this.assets[this.selectedAssetType] || [];

        if (items.length === 0) {
            const msg = this.emptyMsg.cloneNode(true);
            msg.textContent = `No ${this.selectedAssetType}s in project.`;
            this.assetGrid.appendChild(msg);
            return;
        }

        for (const asset of items) {
            const item = document.createElement("div");
            item.style.cssText = `
                width: 80px; background: ${COLORS.galleryItem};
                border: 1px solid ${COLORS.galleryItemBorder};
                border-radius: 4px; overflow: hidden; cursor: grab;
                transition: background 0.15s;
            `;
            item.addEventListener("mouseenter", () => item.style.background = COLORS.galleryItemHover);
            item.addEventListener("mouseleave", () => item.style.background = COLORS.galleryItem);

            // Thumbnail
            const thumb = document.createElement("div");
            thumb.style.cssText = `
                width: 80px; height: 52px; background: #111;
                display: flex; align-items: center; justify-content: center;
                overflow: hidden;
            `;

            if (asset.has_thumbnail) {
                const dirName = this.projectDir.split(/[/\\]/).pop();
                const img = document.createElement("img");
                img.src = api.apiURL(`/ltx-editor/project/${dirName}/thumbnail/${asset.asset_id}`);
                img.style.cssText = "width: 100%; height: 100%; object-fit: cover;";
                img.draggable = false; // Prevent browser from dragging thumbnail URL
                thumb.appendChild(img);
            } else {
                const icon = document.createElement("span");
                icon.style.cssText = `font-size: 20px; color: ${COLORS.textDim};`;
                icon.textContent = this.selectedAssetType === "video" ? "🎬" :
                                   this.selectedAssetType === "image" ? "🖼️" : "🔊";
                thumb.appendChild(icon);
            }

            // Label
            const label = document.createElement("div");
            label.style.cssText = `
                padding: 2px 4px; font-size: 9px; color: ${COLORS.galleryText};
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
                text-align: center;
            `;
            label.textContent = asset.name || "Untitled";
            label.title = asset.name;

            // Metadata line
            const meta = document.createElement("div");
            meta.style.cssText = `
                padding: 0 4px 2px; font-size: 8px; color: ${COLORS.textDim};
                text-align: center;
            `;
            if (asset.asset_type === "video") {
                meta.textContent = `${asset.frame_count}f ${asset.width}×${asset.height}`;
            } else if (asset.asset_type === "image") {
                meta.textContent = `${asset.width}×${asset.height}`;
            } else {
                meta.textContent = `${asset.duration_sec?.toFixed(1) || "?"}s`;
            }

            // Click to preview
            item.addEventListener("click", () => this._previewAsset(asset));

            // Right-click context menu
            item.addEventListener("contextmenu", (e) => {
                e.preventDefault();
                e.stopPropagation();
                this._showContextMenu(e.clientX, e.clientY, [
                    { label: "Rename", action: () => this._startAssetRename(asset, label) },
                ]);
            });

            // Drag support
            item.draggable = true;
            item.addEventListener("dragstart", (e) => {
                // Stash projectDir so the global ComfyUI drop handler can build URLs
                const enrichedAsset = { ...asset, _projectDir: this.projectDir.split(/[/\\]/).pop() };
                e.dataTransfer.setData("application/ltx-asset", JSON.stringify(enrichedAsset));
                e.dataTransfer.effectAllowed = "copy";
            });

            item.append(thumb, label, meta);
            this.assetGrid.appendChild(item);
        }
    }

    _previewAsset(asset) {
        // Remove existing preview
        if (this._previewEl) {
            this._previewEl.remove();
            this._previewEl = null;
        }

        const preview = document.createElement("div");
        preview.style.cssText = `
            position: relative; width: 100%; background: #111;
            border: 1px solid #444; border-radius: 4px; margin-top: 4px;
            overflow: hidden;
        `;

        // Close button
        const closeBtn = document.createElement("button");
        closeBtn.textContent = "✕";
        closeBtn.style.cssText = `
            position: absolute; top: 4px; right: 4px; z-index: 10;
            background: rgba(0,0,0,0.6); border: none; color: #fff;
            cursor: pointer; padding: 2px 6px; border-radius: 3px; font-size: 12px;
        `;
        closeBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            preview.remove();
            this._previewEl = null;
        });
        preview.appendChild(closeBtn);

        // Info bar
        const info = document.createElement("div");
        info.style.cssText = `
            padding: 4px 8px; background: #1a1a1a; font-size: 10px;
            color: ${COLORS.textDim}; border-bottom: 1px solid #333;
        `;
        const parts = [asset.name];
        if (asset.asset_type === "video") {
            parts.push(`${asset.frame_count}f @ ${asset.fps?.toFixed(1)}fps`);
            parts.push(`${asset.width}×${asset.height}`);
            parts.push(`${asset.duration_sec?.toFixed(1)}s`);
        } else if (asset.asset_type === "image") {
            parts.push(`${asset.width}×${asset.height}`);
        } else if (asset.asset_type === "audio") {
            parts.push(`${asset.duration_sec?.toFixed(1)}s`);
        }
        if (asset.prompt) parts.push(`Prompt: ${asset.prompt}`);
        info.textContent = parts.join("  |  ");
        preview.appendChild(info);

        const dirName = this.projectDir.split(/[/\\]/).pop();

        // asset.path is like "media/filename.mp4"
        // ComfyUI /view needs: filename=filename.mp4&subfolder=ltx_projects/DirName/media&type=output
        const assetFileName = asset.path.split(/[/\\]/).pop();
        const assetSubfolder = `ltx_projects/${dirName}/${asset.path.split(/[/\\]/).slice(0, -1).join("/")}`;
        const viewParams = `filename=${encodeURIComponent(assetFileName)}&subfolder=${encodeURIComponent(assetSubfolder)}&type=output`;

        if (asset.asset_type === "video") {
            const video = document.createElement("video");
            video.controls = true;
            video.loop = true;
            video.muted = true;
            video.autoplay = true;
            video.style.cssText = "width: 100%; max-height: 200px; display: block;";
            video.src = api.apiURL(`/view?${viewParams}`);
            preview.appendChild(video);
        } else if (asset.asset_type === "image") {
            const img = document.createElement("img");
            img.style.cssText = "width: 100%; max-height: 200px; object-fit: contain; display: block;";
            img.src = api.apiURL(`/view?${viewParams}`);
            preview.appendChild(img);
        } else if (asset.asset_type === "audio") {
            const audio = document.createElement("audio");
            audio.controls = true;
            audio.style.cssText = "width: 100%; display: block; padding: 8px;";
            audio.src = api.apiURL(`/view?${viewParams}`);
            preview.appendChild(audio);
        }

        // Insert preview above the asset grid
        this.assetGrid.parentElement.insertBefore(preview, this.assetGrid);
        this._previewEl = preview;
    }

    // ── Asset Rename ─────────────────────────────────────────────────
    _startAssetRename(asset, labelEl) {
        const original = asset.name || "Untitled";
        const input = document.createElement("input");
        input.type = "text";
        input.value = original;
        input.style.cssText = `
            font-size: 9px; width: 100%; text-align: center;
            background: #333; color: #ddd; border: 1px solid #666;
            outline: none; padding: 1px 4px; box-sizing: border-box;
        `;
        labelEl.textContent = "";
        labelEl.appendChild(input);
        input.select();
        input.focus();

        const commit = () => {
            const newName = input.value.trim();
            labelEl.textContent = newName || original;
            if (newName && newName !== original) {
                this._renameAsset(asset, newName);
            }
        };
        input.addEventListener("blur", commit);
        input.addEventListener("keydown", (e) => {
            e.stopPropagation();
            if (e.key === "Enter") { input.removeEventListener("blur", commit); commit(); input.blur(); }
            if (e.key === "Escape") { input.removeEventListener("blur", commit); labelEl.textContent = original; input.blur(); }
        });
    }

    async _renameAsset(asset, newName) {
        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/assets/${asset.asset_id}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name: newName }),
            });
            await this._fetchAssets();
        } catch (e) {
            console.warn("[LTX Editor] Failed to rename asset:", e);
        }
    }

    // ── Timecode Helpers ──────────────────────────────────────────────
    _frameToTimecode(frame) {
        if (this._timecodeMode === "frames") return String(frame);
        const fps = this.fps || 24;
        const totalSeconds = frame / fps;
        const h = Math.floor(totalSeconds / 3600);
        const m = Math.floor((totalSeconds % 3600) / 60);
        const s = Math.floor(totalSeconds % 60);
        const f = Math.floor(frame % fps);
        if (h > 0) return `${h}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}:${String(f).padStart(2,"0")}`;
        return `${m}:${String(s).padStart(2,"0")}:${String(f).padStart(2,"0")}`;
    }

    _toggleTimecodeMode() {
        this._timecodeMode = this._timecodeMode === "frames" ? "timecode" : "frames";
        this._renderTimeline();
        this._updateInfoLabel();
        this._updateTransportUI();
    }

    /** Build the track layout array from scene lane counts */
    _buildTrackLayout() {
        const layout = [];
        const scene = this.activeScene;
        const videoLanes = scene?.video_lane_count || 1;
        const audioLanes = scene?.audio_lane_count || 1;
        const vConfigs = scene?.video_lane_configs || [];
        const aConfigs = scene?.audio_lane_configs || [];

        // Preserve collapsed state across rebuilds
        const oldCollapsed = {};
        for (const entry of this._trackLayout) {
            oldCollapsed[entry.type + ":" + entry.laneIndex] = entry.collapsed;
        }

        // Video lanes: highest index at top (foreground on top)
        for (let i = videoLanes - 1; i >= 0; i--) {
            const key = TRACK_TYPE.VIDEO + ":" + i;
            const cfg = vConfigs[i] || {};
            layout.push({
                type: TRACK_TYPE.VIDEO,
                label: cfg.name || (videoLanes > 1 ? `V${i + 1}` : "Video"),
                customName: cfg.name || "",
                laneIndex: i,
                collapsed: oldCollapsed[key] ?? false,
                color: cfg.color || LANE_PALETTE[i % LANE_PALETTE.length],
                locked: cfg.locked || false,
                hidden: cfg.hidden || false,
            });
        }

        // Audio lanes: lowest index at top
        for (let i = 0; i < audioLanes; i++) {
            const key = TRACK_TYPE.AUDIO + ":" + i;
            const cfg = aConfigs[i] || {};
            layout.push({
                type: TRACK_TYPE.AUDIO,
                label: cfg.name || (audioLanes > 1 ? `A${i + 1}` : "Audio"),
                customName: cfg.name || "",
                laneIndex: i,
                collapsed: oldCollapsed[key] ?? false,
                color: cfg.color || LANE_PALETTE[i % LANE_PALETTE.length],
                locked: cfg.locked || false,
                hidden: cfg.hidden || false,
            });
        }

        // Fixed rows (no lock/hide/color)
        layout.push({
            type: TRACK_TYPE.GUIDES,
            label: "Guides",
            customName: "",
            laneIndex: 0,
            collapsed: oldCollapsed[TRACK_TYPE.GUIDES + ":0"] ?? false,
            color: "",
            locked: false,
            hidden: false,
        });
        layout.push({
            type: TRACK_TYPE.PROMPT,
            label: "Prompt",
            customName: "",
            laneIndex: 0,
            collapsed: oldCollapsed[TRACK_TYPE.PROMPT + ":0"] ?? false,
            color: "",
            locked: false,
            hidden: false,
        });

        this._trackLayout = layout;
    }

    /** Find layout index for a video lane */
    _videoLaneLayoutIdx(laneIndex) {
        return this._trackLayout.findIndex(
            e => e.type === TRACK_TYPE.VIDEO && e.laneIndex === laneIndex
        );
    }

    /** Find layout index for an audio lane */
    _audioLaneLayoutIdx(laneIndex) {
        return this._trackLayout.findIndex(
            e => e.type === TRACK_TYPE.AUDIO && e.laneIndex === laneIndex
        );
    }

    /** Find layout index for guides */
    _guidesLayoutIdx() {
        return this._trackLayout.findIndex(e => e.type === TRACK_TYPE.GUIDES);
    }

    /** Find layout index for prompt */
    _promptLayoutIdx() {
        return this._trackLayout.findIndex(e => e.type === TRACK_TYPE.PROMPT);
    }

    /** Get y offset of a layout index accounting for collapsed tracks */
    _trackY(layoutIdx) {
        let y = RULER_HEIGHT;
        for (let i = 0; i < layoutIdx; i++) {
            y += this._trackLayout[i]?.collapsed ? TRACK_COLLAPSED_HEIGHT : TRACK_HEIGHT;
        }
        return y;
    }

    /** Get height of a layout index accounting for collapsed state */
    _trackH(layoutIdx) {
        const entry = this._trackLayout[layoutIdx];
        return entry?.collapsed ? TRACK_COLLAPSED_HEIGHT : TRACK_HEIGHT;
    }

    /** Total height of all tracks */
    _totalTracksHeight() {
        let h = 0;
        for (const entry of this._trackLayout) {
            h += entry.collapsed ? TRACK_COLLAPSED_HEIGHT : TRACK_HEIGHT;
        }
        return h;
    }

    /** Fit timeline zoom to show all content */
    _fitToView() {
        // Find the rightmost content edge
        let maxFrame = 0;
        if (this.activeScene) {
            for (const clip of (this.activeScene.clips || [])) {
                maxFrame = Math.max(maxFrame, clip.timeline_end_frame);
            }
            for (const track of (this.activeScene.audio_tracks || [])) {
                maxFrame = Math.max(maxFrame, track.timeline_end_frame);
            }
            for (const g of (this.activeScene.guide_frames || [])) {
                const idx = g.frame_index === -1 ? this.totalFrames - 1 : g.frame_index;
                maxFrame = Math.max(maxFrame, idx + 1);
            }
            for (const s of (this.activeScene.prompt_sections || [])) {
                maxFrame = Math.max(maxFrame, s.end_frame);
            }
        }
        maxFrame = Math.max(maxFrame, this.totalFrames);

        // Calculate ppf to fit all content with margin (label width already handled by _frameToX)
        const canvas = this.timelineCanvas;
        const rect = canvas.parentElement?.getBoundingClientRect();
        const width = rect ? Math.floor(rect.width) : 400;
        const margin = width * 0.03; // 3% margin on right side
        const availableWidth = width - this._labelW - margin;

        if (maxFrame > 0 && availableWidth > 0) {
            this.pixelsPerFrame = Math.max(0.2, Math.min(40, availableWidth / maxFrame));
            this.scrollX = 0; // Frame 0 starts at label edge
        }
        this._renderTimeline();
    }

    // ── Timeline Rendering ─────────────────────────────────────────────
    _renderTimeline() {
        const canvas = this.timelineCanvas;
        const rect = canvas.parentElement?.getBoundingClientRect();
        const width = rect ? Math.floor(rect.width) : 400;
        const totalH = RULER_HEIGHT + this._totalTracksHeight();
        const canvasH = Math.max(totalH, this._timelineHeight);
        const s = this._uiScale;

        // Set canvas pixel buffer to scaled size, CSS size reflects visual size
        canvas.width = Math.floor(width * s);
        canvas.height = Math.floor(canvasH * s);
        canvas.style.width = width + "px";
        canvas.style.height = (canvasH * s) + "px";

        const ctx = canvas.getContext("2d");
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.save();
        ctx.scale(s, s);

        // Background (logical dimensions)
        ctx.fillStyle = COLORS.bg;
        ctx.fillRect(0, 0, width, canvasH);

        this._drawRuler(ctx, width);
        this._drawTracks(ctx, width);
        this._drawSelection(ctx, width);
        this._drawGuideMarkers(ctx, width);
        this._drawClips(ctx, width);
        this._drawPlayhead(ctx, width);
        this._drawSnapIndicator(ctx, width);
        ctx.restore();
        this._updateInfoLabel();
    }

    get _labelW() { return this.isFullscreen ? LABEL_WIDTH_FS : LABEL_WIDTH; }

    _frameToX(frame) {
        return this._labelW + (frame - this.scrollX) * this.pixelsPerFrame;
    }

    _xToFrame(x) {
        return Math.round((x - this._labelW) / this.pixelsPerFrame + this.scrollX);
    }

    _clampScrollX() {
        const rect = this.timelineCanvas?.parentElement?.getBoundingClientRect();
        const width = rect ? Math.floor(rect.width) : 400;
        const visibleFrames = (width - this._labelW) / this.pixelsPerFrame;
        const maxScroll = Math.max(0, this.totalFrames - visibleFrames + 5);
        this.scrollX = Math.max(0, Math.min(maxScroll, this.scrollX));
    }

    _drawRuler(ctx, width) {
        ctx.fillStyle = COLORS.ruler;
        ctx.fillRect(0, 0, width, RULER_HEIGHT);

        ctx.strokeStyle = COLORS.rulerTick;
        ctx.fillStyle = COLORS.rulerText;
        ctx.font = "9px monospace";
        ctx.textAlign = "center";

        // Determine tick spacing based on zoom
        let majorEvery = 10;
        if (this.pixelsPerFrame < 2) majorEvery = 50;
        else if (this.pixelsPerFrame < 5) majorEvery = 25;
        else if (this.pixelsPerFrame > 10) majorEvery = 5;

        // For timecode mode, adjust major ticks to align with seconds
        if (this._timecodeMode === "timecode") {
            const fps = this.fps || 24;
            if (this.pixelsPerFrame * fps < 80) {
                majorEvery = fps * 5; // every 5 seconds
            } else {
                majorEvery = fps; // every 1 second
            }
        }

        const startFrame = Math.max(0, Math.floor(this.scrollX));
        const endFrame = Math.min(this.totalFrames, Math.ceil(this.scrollX + (width - this._labelW) / this.pixelsPerFrame));

        for (let f = startFrame; f <= endFrame; f++) {
            const x = this._frameToX(f);
            if (x < 0 || x > width) continue;

            if (f % majorEvery === 0) {
                ctx.beginPath();
                ctx.moveTo(x, RULER_HEIGHT - 12);
                ctx.lineTo(x, RULER_HEIGHT);
                ctx.stroke();
                ctx.fillText(this._frameToTimecode(f), x, RULER_HEIGHT - 13);
            } else if (f % (majorEvery / 5) === 0 && this.pixelsPerFrame > 1.5) {
                ctx.beginPath();
                ctx.moveTo(x, RULER_HEIGHT - 6);
                ctx.lineTo(x, RULER_HEIGHT);
                ctx.stroke();
            }
        }
    }

    _drawTracks(ctx, width) {
        const headerW = this.isFullscreen ? LABEL_WIDTH_FS : LABEL_WIDTH;
        const fs = this.isFullscreen;
        for (let i = 0; i < this._trackLayout.length; i++) {
            const entry = this._trackLayout[i];
            const y = this._trackY(i);
            const h = this._trackH(i);
            const collapsed = entry.collapsed;
            const isLane = entry.type === TRACK_TYPE.VIDEO || entry.type === TRACK_TYPE.AUDIO;

            // Track background
            ctx.fillStyle = i % 2 === 0 ? COLORS.track : COLORS.bg;
            ctx.fillRect(0, y, width, h);

            if (collapsed) {
                // Collapsed: just arrow + short label
                ctx.fillStyle = "#555";
                ctx.font = "7px sans-serif";
                ctx.textAlign = "left";
                ctx.fillText(`▸ ${entry.label}`, fs ? 6 : 3, y + h / 2 + 2);
            } else {
                // --- Header layout (left to right) ---
                // Positions scale with fullscreen
                const arrowX = fs ? 6 : 3;
                const iconSize = fs ? 14 : 11;
                let curX = arrowX;

                // 1. Collapse arrow
                ctx.fillStyle = COLORS.textDim;
                ctx.font = `${iconSize}px sans-serif`;
                ctx.textAlign = "left";
                ctx.fillText("▾", curX, y + h / 2 + (fs ? 5 : 4));
                curX += iconSize + 2;

                if (isLane) {
                    // 2. Lock icon — bright red-orange when locked, dim when unlocked
                    if (entry.locked) {
                        // Draw bright background indicator for locked state
                        ctx.fillStyle = "rgba(255, 80, 60, 0.3)";
                        ctx.fillRect(curX - 1, y + 2, iconSize + 1, h - 4);
                    }
                    ctx.fillStyle = entry.locked ? "#ff5544" : "#666";
                    ctx.font = `${iconSize - 2}px sans-serif`;
                    ctx.fillText(entry.locked ? "🔒" : "🔓", curX, y + h / 2 + (fs ? 4 : 3));
                    curX += iconSize + 1;

                    // 3. Hide/Mute icon
                    if (entry.type === TRACK_TYPE.VIDEO) {
                        ctx.fillStyle = entry.hidden ? "#e05050" : "#555";
                        ctx.fillText(entry.hidden ? "🚫" : "👁", curX, y + h / 2 + (fs ? 4 : 3));
                    } else {
                        ctx.fillStyle = entry.hidden ? "#e05050" : "#555";
                        ctx.fillText(entry.hidden ? "🔇" : "🔊", curX, y + h / 2 + (fs ? 4 : 3));
                    }
                    curX += iconSize + 1;

                    // 4. Color bar
                    if (entry.color) {
                        ctx.fillStyle = entry.color;
                        ctx.fillRect(curX, y + 2, 4, h - 4);
                    }
                    curX += 7;
                }

                // 5. Label
                ctx.fillStyle = isLane && entry.hidden ? "#666" : COLORS.textDim;
                ctx.font = fs ? "10px sans-serif" : "8px sans-serif";
                ctx.textAlign = "left";
                const labelText = entry.label;
                const maxLabelW = headerW - curX - 2;
                ctx.save();
                ctx.beginPath();
                ctx.rect(curX, y, maxLabelW, h);
                ctx.clip();
                ctx.fillText(labelText, curX, y + h / 2 + (fs ? 3 : 3));
                ctx.restore();
            }

            // Border
            ctx.strokeStyle = COLORS.trackBorder;
            ctx.beginPath();
            ctx.moveTo(0, y + h);
            ctx.lineTo(width, y + h);
            ctx.stroke();
        }
    }

    _drawSelection(ctx, width) {
        if (this.selectionStart >= this.selectionEnd) return;

        const x1 = this._frameToX(this.selectionStart);
        const x2 = this._frameToX(this.selectionEnd);
        const y = RULER_HEIGHT;
        const h = this._totalTracksHeight();

        // Fill
        ctx.fillStyle = COLORS.selection;
        ctx.fillRect(x1, y, x2 - x1, h);

        // Border
        ctx.strokeStyle = COLORS.selectionBorder;
        ctx.lineWidth = 1;
        ctx.strokeRect(x1, y, x2 - x1, h);

        // Handle bars
        ctx.fillStyle = COLORS.selectionBorder;
        ctx.fillRect(x1 - 2, y, 4, h);
        ctx.fillRect(x2 - 2, y, 4, h);
    }

    _drawGuideMarkers(ctx, width) {
        if (!this.activeScene) return;
        const gi = this._guidesLayoutIdx();
        if (gi < 0 || this._trackLayout[gi].collapsed) return;

        const guides = this.activeScene.guide_frames || [];
        const y = this._trackY(gi);
        const h = this._trackH(gi);

        for (const guide of guides) {
            let idx = guide._previewFrameIndex ?? guide.frame_index;
            if (idx === -1) idx = this.totalFrames - 1;

            const x = this._frameToX(idx);
            if (x < 0 || x > width) continue;

            // Diamond marker
            const isSelectedGuide = this._isSelected("guide", guide.frame_index);
            ctx.fillStyle = isSelectedGuide ? COLORS.guideSelected : COLORS.guide;
            ctx.beginPath();
            ctx.moveTo(x, y + 4);
            ctx.lineTo(x + 8, y + h / 2);
            ctx.lineTo(x, y + h - 4);
            ctx.lineTo(x - 8, y + h / 2);
            ctx.closePath();
            ctx.fill();

            // Label
            ctx.fillStyle = COLORS.text;
            ctx.font = "8px monospace";
            ctx.textAlign = "center";
            ctx.fillText(`f${idx}`, x, y + h + 10);
        }
    }

    _drawClips(ctx, width) {
        if (!this.activeScene) return;

        // Helper: draw trimmed-off ghost region during active trim drag
        const drawTrimGhost = (trimItem, trackY, trackH, color) => {
            if (!this._trimItem || this._trimItem.data !== trimItem) return;
            const item = this._trimItem;
            const isPrompt = item.type === "prompt";
            const curStart = isPrompt ? item.data.start_frame : item.data.timeline_start_frame;
            const curEnd = isPrompt ? item.data.end_frame : item.data.timeline_end_frame;
            ctx.globalAlpha = 0.25;
            ctx.fillStyle = color;
            if (item.edge === "left" && curStart > item.origStart) {
                const ghostX1 = this._frameToX(item.origStart);
                const ghostX2 = this._frameToX(curStart);
                ctx.fillRect(ghostX1 + 1, trackY + 2, ghostX2 - ghostX1 - 1, trackH - 4);
            } else if (item.edge === "right" && curEnd < item.origEnd) {
                const ghostX1 = this._frameToX(curEnd);
                const ghostX2 = this._frameToX(item.origEnd);
                ctx.fillRect(ghostX1, trackY + 2, ghostX2 - ghostX1 - 1, trackH - 4);
            }
            ctx.globalAlpha = 1.0;
        };

        // Video clips (all video lanes)
        const allClips = this.activeScene.clips || [];
        for (let _vli = 0; _vli < this._trackLayout.length; _vli++) {
            const _vlEntry = this._trackLayout[_vli];
            if (_vlEntry.type !== TRACK_TYPE.VIDEO || _vlEntry.collapsed) continue;
            const videoY = this._trackY(_vli);
            const videoH = this._trackH(_vli);
            const laneHidden = _vlEntry.hidden;
            const clips = allClips.filter(c => (c.track_index || 0) === _vlEntry.laneIndex);
            for (const clip of clips) {
                const x1 = this._frameToX(clip.timeline_start_frame);
                const x2 = this._frameToX(clip.timeline_end_frame);
                if (x2 < 0 || x1 > width) continue;

                const isSelectedClip = this._isSelected("clip", clip.clip_id);
                const opacity = clip.opacity ?? 1.0;
                const baseAlpha = laneHidden ? 0.3 : (opacity < 1.0 ? Math.max(0.3, opacity) : 1.0);
                ctx.globalAlpha = baseAlpha;

                // Draw base fill
                ctx.fillStyle = isSelectedClip ? COLORS.clipSelected : COLORS.clip;
                ctx.fillRect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);

                // Thumbnail strip filmstrip (tiled at natural aspect ratio)
                const clipAsset = this._pathToAsset[clip.source_path];
                if (clipAsset && (x2 - x1) > 10) {
                    const strip = this._getOrLoadThumbStrip(clipAsset.asset_id);
                    if (strip && strip.loaded && strip.img.naturalWidth > 0) {
                        ctx.save();
                        ctx.beginPath();
                        ctx.rect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                        ctx.clip();

                        const destH = videoH - 4;
                        // Scale each strip frame to fill track height, preserving aspect ratio
                        const tileW = Math.max(1, Math.round(strip.frameWidth * destH / strip.img.naturalHeight));
                        const totalSourceFrames = clipAsset.frame_count || 1;
                        const srcIn = clip.source_in_frame || 0;
                        const srcOut = clip.source_out_frame || totalSourceFrames;
                        // Tile frames across the clip width
                        const clipPixelW = x2 - x1 - 2;
                        for (let px = 0; px < clipPixelW; px += tileW) {
                            // Map this pixel position to a source frame, then to a strip column
                            const frac = px / clipPixelW;
                            const sourceFrame = srcIn + frac * (srcOut - srcIn);
                            const col = Math.floor(sourceFrame / totalSourceFrames * strip.numFrames);
                            const clampedCol = Math.min(col, strip.numFrames - 1);
                            const sx = clampedCol * strip.frameWidth;
                            const drawW = Math.min(tileW, clipPixelW - px);
                            const srcDrawW = drawW / tileW * strip.frameWidth;
                            ctx.drawImage(strip.img, sx, 0, srcDrawW, strip.img.naturalHeight,
                                          x1 + 1 + px, videoY + 2, drawW, destH);
                        }

                        // Tint overlay — use lane color if available, fallback to default blue
                        const lc = _vlEntry.color || "#3a7ca5";
                        const r = parseInt(lc.slice(1,3),16), g = parseInt(lc.slice(3,5),16), b = parseInt(lc.slice(5,7),16);
                        const tintAlpha = isSelectedClip ? 0.4 : 0.25;
                        ctx.fillStyle = `rgba(${r},${g},${b},${tintAlpha})`;
                        ctx.fillRect(x1 + 1, videoY + 2, clipPixelW, destH);
                        ctx.restore();
                    }
                } else if (_vlEntry.color) {
                    // No thumbnail — apply lane color tint directly on base fill
                    const lc = _vlEntry.color;
                    const r = parseInt(lc.slice(1,3),16), g = parseInt(lc.slice(3,5),16), b = parseInt(lc.slice(5,7),16);
                    const prevAlpha = ctx.globalAlpha;
                    ctx.globalAlpha = prevAlpha * 0.25;
                    ctx.fillStyle = `rgb(${r},${g},${b})`;
                    ctx.fillRect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                    ctx.globalAlpha = prevAlpha;
                }

                if (isSelectedClip) {
                    ctx.strokeStyle = COLORS.clipSelected;
                    ctx.lineWidth = 1;
                    ctx.strokeRect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                }
                ctx.globalAlpha = baseAlpha;

                // Opacity visual: diagonal hash lines when opacity < 100%
                if (opacity < 1.0) {
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 1, videoY + 2, x2 - x1 - 2, videoH - 4);
                    ctx.clip();
                    ctx.strokeStyle = "rgba(0,0,0,0.4)";
                    ctx.lineWidth = 1;
                    const step = 6;
                    for (let lx = x1 - videoH; lx < x2; lx += step) {
                        ctx.beginPath();
                        ctx.moveTo(lx, videoY + videoH - 2);
                        ctx.lineTo(lx + videoH, videoY + 2);
                        ctx.stroke();
                    }
                    ctx.restore();
                }

                // Clip label
                ctx.fillStyle = COLORS.text;
                ctx.font = "9px sans-serif";
                ctx.textAlign = "left";
                const dur = clip.timeline_end_frame - clip.timeline_start_frame;
                let label = this._timecodeMode === "timecode" ? this._frameToTimecode(dur) : `${dur}f`;
                if (opacity < 1.0) label += ` ${Math.round(opacity * 100)}%`;
                ctx.fillText(label, x1 + 4, videoY + videoH / 2 + 3);

                // Permanent trim ghost
                const clipOrigin = clip.source_origin_frame || 0;
                const clipTotal = clip.total_source_frames || 0;
                if (clipTotal > 0) {
                    const leftTrimmed = (clip.source_in_frame || 0) - clipOrigin;
                    const visibleDur = clip.timeline_end_frame - clip.timeline_start_frame;
                    const rightTrimmed = clipTotal - visibleDur - leftTrimmed;
                    if (leftTrimmed > 0) {
                        const ghostX = this._frameToX(clip.timeline_start_frame - leftTrimmed);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = COLORS.clip;
                        ctx.fillRect(ghostX + 1, videoY + 2, x1 - ghostX - 1, videoH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                    if (rightTrimmed > 0) {
                        const ghostX2 = this._frameToX(clip.timeline_end_frame + rightTrimmed);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = COLORS.clip;
                        ctx.fillRect(x2 - 1, videoY + 2, ghostX2 - x2, videoH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                }

                // Active trim drag ghost (during edge-drag)
                if (this.dragType === "trimEdge") drawTrimGhost(clip, videoY, videoH, COLORS.clip);
            }
            ctx.globalAlpha = 1.0;
        }

        // Audio tracks (all audio lanes)
        const allAudioTracks = this.activeScene.audio_tracks || [];
        for (let _ali = 0; _ali < this._trackLayout.length; _ali++) {
            const _alEntry = this._trackLayout[_ali];
            if (_alEntry.type !== TRACK_TYPE.AUDIO || _alEntry.collapsed) continue;
            const audioY = this._trackY(_ali);
            const audioH = this._trackH(_ali);
            const audioLaneHidden = _alEntry.hidden;
            const audioTracks = allAudioTracks.filter(a => (a.lane_index || 0) === _alEntry.laneIndex);
            for (const track of audioTracks) {
                const x1 = this._frameToX(track.timeline_start_frame);
                const x2 = this._frameToX(track.timeline_end_frame);
                if (x2 < 0 || x1 > width) continue;

                const isSelectedAudio = this._isSelected("audio", track.track_id);
                const vol = track.volume ?? 1.0;
                ctx.globalAlpha = audioLaneHidden ? 0.3 : 1.0;
                ctx.fillStyle = (track.muted || audioLaneHidden) ? "#555" : (isSelectedAudio ? COLORS.audioClipSelected : COLORS.audioClip);
                ctx.fillRect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);

                // Waveform visualization
                const audioAsset = this._pathToAsset[track.source_path];
                if (audioAsset && (x2 - x1) > 6) {
                    const waveform = this._getOrLoadWaveform(audioAsset.asset_id);
                    if (waveform && waveform.loaded && waveform.peaks.length > 0) {
                        ctx.save();
                        ctx.beginPath();
                        ctx.rect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                        ctx.clip();

                        const clipW = x2 - x1 - 2;
                        const centerY = audioY + audioH / 2;
                        const halfH = (audioH - 8) / 2;

                        // Map visible source frames to waveform buckets
                        const totalDurFrames = audioAsset.duration_sec * (this.fps || 24);
                        const srcIn = track.source_in_frame || 0;
                        const visibleFrames = track.timeline_end_frame - track.timeline_start_frame;
                        const startFrac = totalDurFrames > 0 ? srcIn / totalDurFrames : 0;
                        const endFrac = totalDurFrames > 0 ? (srcIn + visibleFrames) / totalDurFrames : 1;
                        const startBucket = Math.floor(startFrac * waveform.numBuckets);
                        const endBucket = Math.ceil(endFrac * waveform.numBuckets);
                        const bucketSpan = Math.max(1, endBucket - startBucket);

                        ctx.strokeStyle = track.muted ? "rgba(180,180,180,0.5)" : "rgba(220,255,220,0.9)";
                        ctx.lineWidth = 1;
                        ctx.beginPath();
                        for (let px = 0; px < clipW; px++) {
                            const bi = startBucket + Math.floor(px / clipW * bucketSpan);
                            const peak = waveform.peaks[Math.min(bi, waveform.peaks.length - 1)];
                            if (!peak) continue;
                            const y1 = centerY - peak[1] * halfH;
                            const y2 = centerY - peak[0] * halfH;
                            ctx.moveTo(x1 + 1 + px, y1);
                            ctx.lineTo(x1 + 1 + px, y2);
                        }
                        ctx.stroke();
                        ctx.restore();
                    }
                }

                // Lane color tint (over waveform)
                if (_alEntry.color) {
                    const lc = _alEntry.color;
                    const r = parseInt(lc.slice(1,3),16), g = parseInt(lc.slice(3,5),16), b = parseInt(lc.slice(5,7),16);
                    const prevAlpha = ctx.globalAlpha;
                    ctx.globalAlpha = prevAlpha * 0.2;
                    ctx.fillStyle = `rgb(${r},${g},${b})`;
                    ctx.fillRect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                    ctx.globalAlpha = prevAlpha;
                }

                if (isSelectedAudio) {
                    ctx.strokeStyle = COLORS.audioClipSelected;
                    ctx.lineWidth = 1;
                    ctx.strokeRect(x1 + 1, audioY + 2, x2 - x1 - 2, audioH - 4);
                }

                // Volume indicator: thin bar at bottom of audio clip
                if (vol < 1.0 && !track.muted) {
                    const volBarW = (x2 - x1 - 4) * vol;
                    ctx.fillStyle = "rgba(255,255,255,0.2)";
                    ctx.fillRect(x1 + 2, audioY + audioH - 5, volBarW, 2);
                }

                // Audio label
                if ((x2 - x1) > 30) {
                    ctx.fillStyle = COLORS.text;
                    ctx.font = "8px sans-serif";
                    ctx.textAlign = "left";
                    let aLabel = track.muted ? "M" : "";
                    if (vol < 1.0 && !track.muted) aLabel += `${Math.round(vol * 100)}%`;
                    if (aLabel) ctx.fillText(aLabel, x1 + 4, audioY + audioH / 2 + 3);
                }

                // Permanent trim ghost for audio
                const audioOrigin = track.source_origin_frame || 0;
                const audioTotal = track.total_source_frames || 0;
                if (audioTotal > 0) {
                    const audioLeftTrim = (track.source_in_frame || 0) - audioOrigin;
                    const audioVisibleDur = track.timeline_end_frame - track.timeline_start_frame;
                    const audioRightTrim = audioTotal - audioVisibleDur - audioLeftTrim;
                    if (audioLeftTrim > 0) {
                        const ghostX = this._frameToX(track.timeline_start_frame - audioLeftTrim);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = COLORS.audioClip;
                        ctx.fillRect(ghostX + 1, audioY + 2, x1 - ghostX - 1, audioH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                    if (audioRightTrim > 0) {
                        const ghostX2 = this._frameToX(track.timeline_end_frame + audioRightTrim);
                        ctx.globalAlpha = 0.15;
                        ctx.fillStyle = COLORS.audioClip;
                        ctx.fillRect(x2 - 1, audioY + 2, ghostX2 - x2, audioH - 4);
                        ctx.globalAlpha = 1.0;
                    }
                }

                // Active trim drag ghost
                if (this.dragType === "trimEdge") drawTrimGhost(track, audioY, audioH, COLORS.audioClip);
            }
            ctx.globalAlpha = 1.0;
        }

        // Prompt sections
        const pi = this._promptLayoutIdx();
        if (pi >= 0 && !this._trackLayout[pi].collapsed) {
            const sections = this.activeScene.prompt_sections || [];
            const promptY = this._trackY(pi);
            const promptH = this._trackH(pi);
            for (let si = 0; si < sections.length; si++) {
                const section = sections[si];
                const x1 = this._frameToX(section.start_frame);
                const x2 = this._frameToX(section.end_frame);
                if (x2 < 0 || x1 > width) continue;

                const isSelected = this._isSelected("prompt", si) ||
                    (this._selectedPromptIdx !== null &&
                    this._selectedPromptIdx < sections.length &&
                    sections[this._selectedPromptIdx] === section);

                ctx.fillStyle = isSelected ? "rgba(180, 120, 255, 0.4)" : COLORS.promptSection;
                ctx.fillRect(x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4);

                ctx.strokeStyle = isSelected ? "rgba(180, 120, 255, 0.9)" : COLORS.promptBorder;
                ctx.lineWidth = 1;
                ctx.strokeRect(x1 + 1, promptY + 2, x2 - x1 - 2, promptH - 4);

                // Prompt text label (truncated)
                if (section.prompt && (x2 - x1) > 20) {
                    ctx.fillStyle = COLORS.text;
                    ctx.font = "9px sans-serif";
                    ctx.textAlign = "left";
                    ctx.save();
                    ctx.beginPath();
                    ctx.rect(x1 + 3, promptY + 2, x2 - x1 - 6, promptH - 4);
                    ctx.clip();
                    ctx.fillText(section.prompt, x1 + 4, promptY + promptH / 2 + 3);
                    ctx.restore();
                }

                // Trim ghost
                if (this.dragType === "trimEdge") drawTrimGhost(section, promptY, promptH, "rgba(180, 120, 255, 0.5)");
            }
        }
    }

    _drawPlayhead(ctx, width) {
        const x = this._frameToX(this.playhead);
        if (x < 0 || x > width) return;

        const totalH = RULER_HEIGHT + this._totalTracksHeight();
        ctx.strokeStyle = COLORS.playhead;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, totalH);
        ctx.stroke();

        // Triangle at top
        ctx.fillStyle = COLORS.playhead;
        ctx.beginPath();
        ctx.moveTo(x - 6, 0);
        ctx.lineTo(x + 6, 0);
        ctx.lineTo(x, 8);
        ctx.closePath();
        ctx.fill();
    }

    _drawSnapIndicator(ctx, width) {
        if (this._snapIndicator === null) return;
        const x = this._frameToX(this._snapIndicator);
        if (x < 0 || x > width) return;

        const totalH = RULER_HEIGHT + this._totalTracksHeight();
        ctx.strokeStyle = "#ffff00";
        ctx.lineWidth = 1;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, totalH);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    _updateInfoLabel() {
        const parts = [];
        if (this.selectionStart < this.selectionEnd) {
            const count = this.selectionEnd - this.selectionStart;
            parts.push(`Selection: ${this._frameToTimecode(this.selectionStart)}-${this._frameToTimecode(this.selectionEnd)} (${this._frameToTimecode(count)})`);
        } else {
            parts.push("Click & drag to select frames");
        }
        parts.push(`Playhead: ${this._frameToTimecode(this.playhead)}`);
        parts.push(`Total: ${this._frameToTimecode(this.totalFrames)}`);
        this.infoLabel.textContent = parts.join("  |  ");
    }

    // ── Hit Testing ──────────────────────────────────────────────────
    /** Hit-test track header area — returns { layoutIdx, zone } or null */
    _hitTestTrackHeader(x, y) {
        const headerWidth = this.isFullscreen ? LABEL_WIDTH_FS : LABEL_WIDTH;
        if (x > headerWidth) return null;
        const fs = this.isFullscreen;
        const iconSize = fs ? 14 : 11;
        for (let i = 0; i < this._trackLayout.length; i++) {
            const ty = this._trackY(i);
            const th = this._trackH(i);
            if (y >= ty && y < ty + th) {
                const entry = this._trackLayout[i];
                const isLane = entry.type === TRACK_TYPE.VIDEO || entry.type === TRACK_TYPE.AUDIO;
                if (entry.collapsed || !isLane) {
                    return { layoutIdx: i, zone: "collapse" };
                }
                // Zone detection (left to right) matching _drawTracks layout
                const arrowX = fs ? 6 : 3;
                let zoneEnd = arrowX + iconSize + 2;
                if (x < zoneEnd) return { layoutIdx: i, zone: "collapse" };
                zoneEnd += iconSize + 1;
                if (x < zoneEnd) return { layoutIdx: i, zone: "lock" };
                zoneEnd += iconSize + 1;
                if (x < zoneEnd) return { layoutIdx: i, zone: "hide" };
                return { layoutIdx: i, zone: "label" };
            }
        }
        return null;
    }

    _hitTestClip(x, y) {
        if (!this.activeScene) return null;
        const clips = this.activeScene.clips || [];

        for (let li = 0; li < this._trackLayout.length; li++) {
            const entry = this._trackLayout[li];
            if (entry.type !== TRACK_TYPE.VIDEO || entry.collapsed) continue;

            const trackY = this._trackY(li);
            const trackH = this._trackH(li);
            if (y < trackY || y > trackY + trackH) continue;

            for (const clip of clips) {
                if ((clip.track_index || 0) !== entry.laneIndex) continue;
                const x1 = this._frameToX(clip.timeline_start_frame);
                const x2 = this._frameToX(clip.timeline_end_frame);
                if (x >= x1 && x <= x2) {
                    return { type: "clip", id: clip.clip_id, data: clip };
                }
            }
        }
        return null;
    }

    _hitTestAudio(x, y) {
        if (!this.activeScene) return null;
        const tracks = this.activeScene.audio_tracks || [];

        for (let li = 0; li < this._trackLayout.length; li++) {
            const entry = this._trackLayout[li];
            if (entry.type !== TRACK_TYPE.AUDIO || entry.collapsed) continue;

            const trackY = this._trackY(li);
            const trackH = this._trackH(li);
            if (y < trackY || y > trackY + trackH) continue;

            for (const track of tracks) {
                if ((track.lane_index || 0) !== entry.laneIndex) continue;
                const x1 = this._frameToX(track.timeline_start_frame);
                const x2 = this._frameToX(track.timeline_end_frame);
                if (x >= x1 && x <= x2) {
                    return { type: "audio", id: track.track_id, data: track };
                }
            }
        }
        return null;
    }

    _hitTestGuide(x, y) {
        if (!this.activeScene) return null;
        const gi = this._guidesLayoutIdx();
        if (gi < 0 || this._trackLayout[gi].collapsed) return null;
        const guides = this.activeScene.guide_frames || [];
        const trackY = this._trackY(gi);
        const trackH = this._trackH(gi);
        if (y < trackY || y > trackY + trackH) return null;

        for (const guide of guides) {
            let idx = guide.frame_index;
            if (idx === -1) idx = this.totalFrames - 1;
            const gx = this._frameToX(idx);
            if (Math.abs(x - gx) <= 10) {
                return { type: "guide", id: guide.frame_index, data: guide };
            }
        }
        return null;
    }

    _hitTestPrompt(x, y) {
        if (!this.activeScene) return null;
        const pli = this._promptLayoutIdx();
        if (pli < 0 || this._trackLayout[pli].collapsed) return null;
        const sections = this.activeScene.prompt_sections || [];
        const trackY = this._trackY(pli);
        const trackH = this._trackH(pli);
        if (y < trackY || y > trackY + trackH) return null;

        for (let i = 0; i < sections.length; i++) {
            const section = sections[i];
            const x1 = this._frameToX(section.start_frame);
            const x2 = this._frameToX(section.end_frame);
            if (x >= x1 && x <= x2) {
                return { type: "prompt", id: i, data: section };
            }
        }
        return null;
    }

    _hitTestItem(x, y) {
        return this._hitTestClip(x, y) || this._hitTestAudio(x, y) || this._hitTestGuide(x, y) || this._hitTestPrompt(x, y);
    }

    /** Detect if the mouse is near the left or right edge of a clip/audio track for trimming.
     *  Returns { type, id, data, edge: "left"|"right" } or null. */
    _hitTestEdge(x, y) {
        const edgePx = 6;
        if (!this.activeScene) return null;

        // Check clips (all video lanes)
        for (let li = 0; li < this._trackLayout.length; li++) {
            const entry = this._trackLayout[li];
            if (entry.type !== TRACK_TYPE.VIDEO || entry.collapsed) continue;
            const clipTrackY = this._trackY(li);
            const clipTrackH = this._trackH(li);
            if (y >= clipTrackY && y <= clipTrackY + clipTrackH) {
                for (const clip of (this.activeScene.clips || [])) {
                    if ((clip.track_index || 0) !== entry.laneIndex) continue;
                    const x1 = this._frameToX(clip.timeline_start_frame);
                    const x2 = this._frameToX(clip.timeline_end_frame);
                    if (Math.abs(x - x1) < edgePx) return { type: "clip", id: clip.clip_id, data: clip, edge: "left" };
                    if (Math.abs(x - x2) < edgePx) return { type: "clip", id: clip.clip_id, data: clip, edge: "right" };
                }
            }
        }

        // Check audio tracks (all audio lanes)
        for (let li = 0; li < this._trackLayout.length; li++) {
            const entry = this._trackLayout[li];
            if (entry.type !== TRACK_TYPE.AUDIO || entry.collapsed) continue;
            const audioTrackY = this._trackY(li);
            const audioTrackH = this._trackH(li);
            if (y >= audioTrackY && y <= audioTrackY + audioTrackH) {
                for (const track of (this.activeScene.audio_tracks || [])) {
                    if ((track.lane_index || 0) !== entry.laneIndex) continue;
                    const x1 = this._frameToX(track.timeline_start_frame);
                    const x2 = this._frameToX(track.timeline_end_frame);
                    if (Math.abs(x - x1) < edgePx) return { type: "audio", id: track.track_id, data: track, edge: "left" };
                    if (Math.abs(x - x2) < edgePx) return { type: "audio", id: track.track_id, data: track, edge: "right" };
                }
            }
        }

        // Check prompt sections
        const pli = this._promptLayoutIdx();
        if (pli >= 0 && !this._trackLayout[pli].collapsed) {
            const promptTrackY = this._trackY(pli);
            const promptTrackH = this._trackH(pli);
            if (y >= promptTrackY && y <= promptTrackY + promptTrackH) {
                const sections = this.activeScene.prompt_sections || [];
                for (let i = 0; i < sections.length; i++) {
                    const section = sections[i];
                    const x1 = this._frameToX(section.start_frame);
                    const x2 = this._frameToX(section.end_frame);
                    if (Math.abs(x - x1) < edgePx) return { type: "prompt", id: i, data: section, edge: "left" };
                    if (Math.abs(x - x2) < edgePx) return { type: "prompt", id: i, data: section, edge: "right" };
                }
            }
        }

        return null;
    }

    /** Check if an item is in the current selection. */
    _isSelected(type, id) {
        return this.selectedItems.some(s => s.type === type && s.id === id);
    }

    /** Clear the entire selection. */
    _clearSelection() {
        this.selectedItems = [];
        this.selectedItem = null;
    }

    /** Select a single item (replaces selection). */
    _selectItem(hit) {
        this.selectedItems = [hit];
        this.selectedItem = hit;
    }

    /** Toggle an item in the selection (Ctrl+click). */
    _toggleSelectItem(hit) {
        const idx = this.selectedItems.findIndex(s => s.type === hit.type && s.id === hit.id);
        if (idx >= 0) {
            this.selectedItems.splice(idx, 1);
            this.selectedItem = this.selectedItems[this.selectedItems.length - 1] || null;
        } else {
            this.selectedItems.push(hit);
            this.selectedItem = hit;
        }
    }

    /** Add an item to the selection (Shift+click). */
    _addToSelection(hit) {
        if (!this._isSelected(hit.type, hit.id)) {
            this.selectedItems.push(hit);
        }
        this.selectedItem = hit;
    }

    /** Snap a frame to nearby edges (clip edges, guide frames, playhead, selection bounds).
     *  Returns the snapped frame, or the original if no snap.
     *  Sets this._snapIndicator for visual feedback. */
    _snapFrame(frame, excludeIds = []) {
        if (!this.snappingEnabled || !this.activeScene) {
            this._snapIndicator = null;
            return frame;
        }

        const threshold = this._snapThreshold;
        const candidates = [];

        // Playhead
        candidates.push(this.playhead);

        // Selection bounds
        if (this.selectionStart < this.selectionEnd) {
            candidates.push(this.selectionStart, this.selectionEnd);
        }

        // Clip edges
        for (const clip of (this.activeScene.clips || [])) {
            if (excludeIds.includes(clip.clip_id)) continue;
            candidates.push(clip.timeline_start_frame, clip.timeline_end_frame);
        }

        // Audio track edges
        for (const track of (this.activeScene.audio_tracks || [])) {
            if (excludeIds.includes(track.track_id)) continue;
            candidates.push(track.timeline_start_frame, track.timeline_end_frame);
        }

        // Guide frames
        for (const g of (this.activeScene.guide_frames || [])) {
            const idx = g.frame_index === -1 ? this.totalFrames - 1 : g.frame_index;
            candidates.push(idx);
        }

        // Prompt section edges
        for (let i = 0; i < (this.activeScene.prompt_sections || []).length; i++) {
            if (excludeIds.includes(i)) continue;
            const s = this.activeScene.prompt_sections[i];
            candidates.push(s.start_frame, s.end_frame);
        }

        // Frame 0 and last frame
        candidates.push(0, this.totalFrames);

        let best = frame;
        let bestDist = threshold + 1;
        for (const c of candidates) {
            const d = Math.abs(frame - c);
            if (d < bestDist) {
                bestDist = d;
                best = c;
            }
        }

        this._snapIndicator = (bestDist <= threshold) ? best : null;
        return (bestDist <= threshold) ? best : frame;
    }

    // ── Timeline Events ────────────────────────────────────────────────
    _setupTimelineEvents() {
        const canvas = this.timelineCanvas;

        canvas.addEventListener("mousedown", (e) => {
            // Only handle left-click (button 0) for selection/playhead
            if (e.button !== 0) return;

            // Close context menu on any click
            this._hideContextMenu();

            const { x, y } = this._canvasMouseCoords(e);
            const frame = Math.max(0, Math.min(this.totalFrames, this._xToFrame(x)));

            if (y < RULER_HEIGHT) {
                // Click on ruler = move playhead
                this.playhead = frame;
                this.isDragging = true;
                this.dragType = "playhead";
                if (this.isPlaying) this._stopPlayback();
                this._renderViewportFrame();
            } else if (this._hitTestTrackHeader(x, y)) {
                const headerHit = this._hitTestTrackHeader(x, y);
                const entry = this._trackLayout[headerHit.layoutIdx];
                switch (headerHit.zone) {
                    case "collapse":
                        entry.collapsed = !entry.collapsed;
                        break;
                    case "lock":
                        entry.locked = !entry.locked;
                        this._saveLaneConfig();
                        break;
                    case "hide":
                        entry.hidden = !entry.hidden;
                        this._saveLaneConfig();
                        break;
                }
                this._renderTimeline();
                this._renderViewportFrame();
            } else {
                // Selection is set only via I/O shortcuts — no mouse selection drag
                if (this._razorMode) {
                    // Razor mode: split clip or audio at click position
                    const hit = this._hitTestItem(x, y);
                    if (hit && (hit.type === "clip" || hit.type === "audio")) {
                        this._splitClipAtFrame(hit, frame);
                    }
                    return;
                } else {
                    // Check if near clip/audio/prompt edges for trimming
                    const edgeHit = this._hitTestEdge(x, y);
                    if (edgeHit) {
                        // Block trim on locked lanes
                        if (edgeHit.type === "clip" && this._isLaneLocked(TRACK_TYPE.VIDEO, edgeHit.data.track_index || 0)) return;
                        if (edgeHit.type === "audio" && this._isLaneLocked(TRACK_TYPE.AUDIO, edgeHit.data.lane_index || 0)) return;
                        this._pushUndo("trim");
                        const isPrompt = edgeHit.type === "prompt";
                        this._trimItem = {
                            ...edgeHit,
                            origStart: isPrompt ? edgeHit.data.start_frame : edgeHit.data.timeline_start_frame,
                            origEnd: isPrompt ? edgeHit.data.end_frame : edgeHit.data.timeline_end_frame,
                            origSourceIn: edgeHit.data.source_in_frame || 0,
                            origSourceOut: edgeHit.data.source_out_frame || ((isPrompt ? edgeHit.data.end_frame : edgeHit.data.timeline_end_frame) - (isPrompt ? edgeHit.data.start_frame : edgeHit.data.timeline_start_frame)),
                        };
                        this.isDragging = true;
                        this.dragType = "trimEdge";
                        this._dragStartFrame = frame;
                        this._selectItem(edgeHit);
                        this._renderTimeline();
                        return;
                    }

                    // Check if clicking on a timeline item
                    const hit = this._hitTestItem(x, y);
                    if (hit) {
                        if (e.ctrlKey || e.metaKey) {
                            // Ctrl+click = toggle item in selection
                            this._toggleSelectItem(hit);
                        } else if (e.shiftKey) {
                            // Shift+click = add to selection
                            this._addToSelection(hit);
                        } else if (!this._isSelected(hit.type, hit.id)) {
                            // Plain click on unselected item = select only this
                            this._selectItem(hit);
                        } else {
                            // Plain click on already-selected item = keep selection (for drag)
                            this.selectedItem = hit;
                        }
                        this._hideItemEditor(); // Will show on mouseup if no drag
                        // Block drag if any selected item is on a locked lane
                        const anyLocked = this.selectedItems.some(s => {
                            if (s.type === "clip") return this._isLaneLocked(TRACK_TYPE.VIDEO, s.data.track_index || 0);
                            if (s.type === "audio") return this._isLaneLocked(TRACK_TYPE.AUDIO, s.data.lane_index || 0);
                            return false;
                        });
                        if (anyLocked) return;
                        this._pushUndo("move items"); // Capture BEFORE drag modifies data
                        this.isDragging = true;
                        this.dragType = "moveItem";
                        this._dragStartFrame = frame;
                        this._lastSnappedDelta = 0; // Track snapped delta for commit
                        this._dragItemOrigStart = hit.data.timeline_start_frame ?? hit.data.start_frame ?? hit.data.frame_index ?? 0;
                        this._dragItemOrigEnd = hit.data.timeline_end_frame ?? hit.data.end_frame ?? this._dragItemOrigStart;
                        // Store original positions + lane info for all selected items (group move)
                        this._dragItemsOrig = this.selectedItems.map(s => ({
                            type: s.type, id: s.id, data: s.data,
                            origStart: s.data.timeline_start_frame ?? s.data.start_frame ?? s.data.frame_index ?? 0,
                            origEnd: s.data.timeline_end_frame ?? s.data.end_frame ?? (s.data.timeline_start_frame ?? s.data.start_frame ?? s.data.frame_index ?? 0),
                            origLane: s.type === "clip" ? (s.data.track_index || 0) : (s.type === "audio" ? (s.data.lane_index || 0) : 0),
                        }));
                        this._dragLaneChanged = false;
                        // Snapshot ALL clip/audio lanes for swap preview
                        this._origAllClipLanes = {};
                        for (const c of (this.activeScene?.clips || [])) {
                            this._origAllClipLanes[c.clip_id] = c.track_index || 0;
                        }
                        this._origAllAudioLanes = {};
                        for (const a of (this.activeScene?.audio_tracks || [])) {
                            this._origAllAudioLanes[a.track_id] = a.lane_index || 0;
                        }
                    } else {
                        // Click on empty space — deselect all
                        this._clearSelection();
                        this._hideItemEditor();
                    }
                }
            }
            this._renderTimeline();
        });

        canvas.addEventListener("mousemove", (e) => {
            const { x, y } = this._canvasMouseCoords(e);

            if (!this.isDragging) {
                // Update cursor based on position
                if (this._razorMode) {
                    canvas.style.cursor = "crosshair";
                } else if (this._hitTestEdge(x, y)) {
                    canvas.style.cursor = "ew-resize";
                } else if (this._hitTestItem(x, y)) {
                    canvas.style.cursor = "grab";
                } else {
                    canvas.style.cursor = "crosshair";
                }
                return;
            }

            const frame = Math.max(0, Math.min(this.totalFrames, this._xToFrame(x)));

            if (this.dragType === "playhead") {
                this.playhead = frame;
                this._renderViewportFrame();
            } else if (this.dragType === "trimEdge" && this._trimItem) {
                canvas.style.cursor = "ew-resize";
                const snappedFrame = this._snapFrame(frame, [this._trimItem.id]);
                const item = this._trimItem;

                if (item.type === "prompt") {
                    // Prompts have no source media — just resize freely
                    if (item.edge === "left") {
                        item.data.start_frame = Math.max(0, Math.min(item.origEnd - 1, snappedFrame));
                    } else {
                        item.data.end_frame = Math.max(item.origStart + 1, Math.min(this.totalFrames, snappedFrame));
                    }
                } else {
                    // Clips and audio — clamp to source media bounds
                    const totalSourceFrames = (item.origEnd - item.origStart) + item.origSourceIn;
                    if (item.edge === "left") {
                        const delta = Math.max(-item.origSourceIn, snappedFrame - item.origStart);
                        const newStart = Math.max(0, Math.min(item.origEnd - 1, item.origStart + delta));
                        item.data.timeline_start_frame = newStart;
                        const sourceInDelta = newStart - item.origStart;
                        if (item.type === "clip") {
                            item.data.source_in_frame = Math.max(0, item.origSourceIn + sourceInDelta);
                        } else if (item.type === "audio") {
                            item.data.source_in_frame = Math.max(0, item.origSourceIn + sourceInDelta);
                        }
                    } else {
                        // Clamp: can't extend past total source duration
                        const maxEnd = item.origStart + totalSourceFrames - item.origSourceIn;
                        const newEnd = Math.max(item.origStart + 1, Math.min(maxEnd, snappedFrame));
                        item.data.timeline_end_frame = newEnd;
                        if (item.type === "clip") {
                            item.data.source_out_frame = item.origSourceOut + (newEnd - item.origEnd);
                        }
                    }
                }
            } else if (this.dragType === "moveItem" && this.selectedItems.length > 0) {
                canvas.style.cursor = "grabbing";
                const rawDelta = frame - this._dragStartFrame;
                // Apply snapping to the primary item's new position, compute adjusted delta
                const excludeIds = this.selectedItems.map(s => s.id);
                const primaryNewStart = Math.max(0, this._dragItemOrigStart + rawDelta);
                const snappedStart = this._snapFrame(primaryNewStart, excludeIds);
                const frameDelta = snappedStart - this._dragItemOrigStart;
                this._lastSnappedDelta = frameDelta; // Store for mouseup commit
                // Also snap the end edge of the primary item
                if (this.selectedItem && (this.selectedItem.type === "clip" || this.selectedItem.type === "audio")) {
                    const primaryEnd = snappedStart + (this._dragItemOrigEnd - this._dragItemOrigStart);
                    const snappedEnd = this._snapFrame(primaryEnd, excludeIds);
                    if (snappedEnd !== primaryEnd && this._snapIndicator === null) {
                        // End edge snapped — adjust delta
                        const endDelta = snappedEnd - this._dragItemOrigEnd;
                        // Use end-snap if it's closer
                        if (Math.abs(endDelta - rawDelta) < Math.abs(frameDelta - rawDelta - this._dragItemOrigStart)) {
                            // Keep start-edge snap if active, otherwise use end-edge snap
                        }
                    }
                }
                // Detect lane from Y position for cross-lane drag
                let hoverLaneType = null;
                let hoverLaneIndex = -1;
                for (let li = 0; li < this._trackLayout.length; li++) {
                    const entry = this._trackLayout[li];
                    const ty = this._trackY(li);
                    const th = this._trackH(li);
                    if (y >= ty && y < ty + th) {
                        hoverLaneType = entry.type;
                        hoverLaneIndex = entry.laneIndex;
                        break;
                    }
                }

                // Step 1: Restore ALL clips/audio to their original lanes
                for (const c of (this.activeScene?.clips || [])) {
                    if (this._origAllClipLanes && this._origAllClipLanes[c.clip_id] !== undefined) {
                        c.track_index = this._origAllClipLanes[c.clip_id];
                    }
                }
                for (const a of (this.activeScene?.audio_tracks || [])) {
                    if (this._origAllAudioLanes && this._origAllAudioLanes[a.track_id] !== undefined) {
                        a.lane_index = this._origAllAudioLanes[a.track_id];
                    }
                }

                // Step 2: Determine dragged items' target lane
                const draggedClipIds = new Set((this._dragItemsOrig || []).filter(o => o.type === "clip").map(o => o.id));
                const draggedAudioIds = new Set((this._dragItemsOrig || []).filter(o => o.type === "audio").map(o => o.id));
                this._dragLaneChanged = false;

                // Step 3: Move all selected items + apply swap preview
                for (const orig of (this._dragItemsOrig || [])) {
                    if (orig.type === "clip" || orig.type === "audio") {
                        const duration = orig.origEnd - orig.origStart;
                        const newStart = Math.max(0, orig.origStart + frameDelta);
                        orig.data.timeline_start_frame = newStart;
                        orig.data.timeline_end_frame = newStart + duration;
                        // Lane swap preview
                        if (orig.type === "clip" && hoverLaneType === TRACK_TYPE.VIDEO && hoverLaneIndex >= 0) {
                            if (hoverLaneIndex !== orig.origLane) {
                                orig.data.track_index = hoverLaneIndex;
                                this._dragLaneChanged = true;
                                // Swap: move all non-dragged clips on target lane to source lane
                                for (const c of (this.activeScene?.clips || [])) {
                                    if (!draggedClipIds.has(c.clip_id) && c.track_index === hoverLaneIndex) {
                                        c.track_index = orig.origLane;
                                    }
                                }
                            }
                        } else if (orig.type === "audio" && hoverLaneType === TRACK_TYPE.AUDIO && hoverLaneIndex >= 0) {
                            if (hoverLaneIndex !== orig.origLane) {
                                orig.data.lane_index = hoverLaneIndex;
                                this._dragLaneChanged = true;
                                for (const a of (this.activeScene?.audio_tracks || [])) {
                                    if (!draggedAudioIds.has(a.track_id) && a.lane_index === hoverLaneIndex) {
                                        a.lane_index = orig.origLane;
                                    }
                                }
                            }
                        }
                    } else if (orig.type === "guide") {
                        const newIdx = Math.max(0, Math.min(this.totalFrames - 1, orig.origStart + frameDelta));
                        orig.data._previewFrameIndex = newIdx;
                    } else if (orig.type === "prompt") {
                        const duration = orig.origEnd - orig.origStart;
                        const newStart = Math.max(0, orig.origStart + frameDelta);
                        orig.data.start_frame = newStart;
                        orig.data.end_frame = newStart + duration;
                    }
                }
            }

            this._renderTimeline();
        });

        const onMouseUp = (e) => {
            if (!this.isDragging) return;
            const wasDragType = this.dragType;
            this.isDragging = false;
            this.dragType = null;
            this._snapIndicator = null;

            if (wasDragType === "trimEdge" && this._trimItem) {
                // Commit trim to server
                this._commitTrim(this._trimItem);
                this._trimItem = null;
                canvas.style.cursor = "crosshair";
            } else if (wasDragType === "moveItem" && this.selectedItems.length > 0) {
                // Use the snapped delta (stored during mousemove), not raw mouse position
                const frameDelta = this._lastSnappedDelta || 0;

                if (frameDelta !== 0 || this._dragLaneChanged) {
                    this._commitItemMove(frameDelta);
                } else {
                    // Click without drag = show properties editor (single item only)
                    // Remove the undo entry since nothing changed
                    if (this._undoStack.length > 0) this._undoStack.pop();
                    if (this.selectedItems.length === 1) {
                        if (this.selectedItem?.type === "prompt") {
                            // Show prompt text editor for prompt sections
                            const sections = this.activeScene?.prompt_sections || [];
                            const idx = this.selectedItem.id;
                            if (idx >= 0 && idx < sections.length) {
                                this._selectedPromptIdx = idx;
                                this._showPromptEditor(sections[idx], idx);
                            }
                        } else {
                            this._showItemEditor();
                        }
                    }
                }
                canvas.style.cursor = "grab";
            } else {
                // Normalize selection direction
                if (this.selectionStart > this.selectionEnd) {
                    [this.selectionStart, this.selectionEnd] = [this.selectionEnd, this.selectionStart];
                }

                // Update hidden widgets
                this._setWidgetValue("selection_start", this.selectionStart);
                this._setWidgetValue("selection_end", this.selectionEnd);
            }

            this._renderTimeline();
        };

        canvas.addEventListener("mouseup", onMouseUp);
        canvas.addEventListener("mouseleave", onMouseUp);

        // Scroll to pan
        canvas.addEventListener("wheel", (e) => {
            e.preventDefault();
            if (e.ctrlKey) {
                // Zoom
                this._zoom(e.deltaY < 0 ? 1 : -1);
            } else {
                // Pan
                this.scrollX += e.deltaY / this.pixelsPerFrame * 3;
                this._clampScrollX();
                this._renderTimeline();
            }
        });

        // Drop assets onto timeline
        canvas.addEventListener("dragover", (e) => {
            e.preventDefault();
            e.stopPropagation(); // Prevent ComfyUI from showing its own drop indicator
            e.dataTransfer.dropEffect = "copy";
        });

        canvas.addEventListener("drop", (e) => {
            e.preventDefault();
            e.stopPropagation(); // Prevent ComfyUI from also handling this drop
            const assetData = e.dataTransfer.getData("application/ltx-asset");
            if (!assetData) return;

            try {
                const asset = JSON.parse(assetData);
                const { x, y } = this._canvasMouseCoords(e);
                const frame = Math.max(0, this._xToFrame(x));

                this._handleAssetDrop(asset, frame, y);
            } catch (err) {
                console.warn("[LTX Editor] Drop failed:", err);
            }
        });

        // Double-click on Prompt track = create prompt section
        canvas.addEventListener("dblclick", (e) => {
            const { x, y } = this._canvasMouseCoords(e);
            const _pli = this._promptLayoutIdx();
            if (_pli >= 0) {
                const promptTrackY = this._trackY(_pli);
                if (y >= promptTrackY && y <= promptTrackY + this._trackH(_pli)) {
                    const frame = Math.max(0, Math.min(this.totalFrames, this._xToFrame(x)));
                    this._createPromptSection(frame);
                }
            }
        });

        // Click on Prompt track = select prompt section
        canvas.addEventListener("click", (e) => {
            // Prompt deselection is handled by the unified mousedown/mouseup system
            // When clicking empty space, deselect prompt editor too
            const { x, y } = this._canvasMouseCoords(e);

            if (this._selectedPromptIdx !== null) {
                const _pli2 = this._promptLayoutIdx();
                if (_pli2 >= 0) {
                    const promptTrackY = this._trackY(_pli2);
                    if (y < promptTrackY || y > promptTrackY + this._trackH(_pli2)) {
                        this._selectedPromptIdx = null;
                        this._hidePromptEditor();
                        this._renderTimeline();
                    }
                }
            }
        });

        // Right-click on timeline — custom context menu
        canvas.addEventListener("contextmenu", (e) => {
            e.preventDefault();

            const { x, y } = this._canvasMouseCoords(e);
            const frame = Math.max(0, this._xToFrame(x));

            const menuItems = [];

            // Check for track header right-click (lane management)
            const headerHit = this._hitTestTrackHeader(x, y);
            if (headerHit) {
                const entry = this._trackLayout[headerHit.layoutIdx];
                if (entry.type === TRACK_TYPE.VIDEO || entry.type === TRACK_TYPE.AUDIO) {
                    const isVideo = entry.type === TRACK_TYPE.VIDEO;
                    const laneCount = isVideo
                        ? (this.activeScene?.video_lane_count || 1)
                        : (this.activeScene?.audio_lane_count || 1);
                    const label = isVideo ? "Video" : "Audio";

                    menuItems.push({ label: "Rename Lane", action: () => this._startLaneRename(headerHit.layoutIdx) });
                    menuItems.push({ label: `Add ${label} Lane`, action: () => this._addLane(entry.type) });
                    if (laneCount > 1) {
                        // Only allow removing if lane is empty
                        const hasItems = isVideo
                            ? (this.activeScene?.clips || []).some(c => (c.track_index || 0) === entry.laneIndex)
                            : (this.activeScene?.audio_tracks || []).some(a => (a.lane_index || 0) === entry.laneIndex);
                        if (hasItems) {
                            menuItems.push({ label: `Remove ${label} Lane (move items)`, action: () => this._removeLaneWithItems(entry.type, entry.laneIndex), danger: true });
                        } else {
                            menuItems.push({ label: `Remove ${label} Lane`, action: () => this._removeLane(entry.type, entry.laneIndex), danger: true });
                        }
                    }
                }
                if (menuItems.length > 0) {
                    this._showContextMenu(e.clientX, e.clientY, menuItems);
                }
                return;
            }

            // Check for timeline item hits
            const hit = this._hitTestItem(x, y);
            if (hit) {
                if (!this._isSelected(hit.type, hit.id)) {
                    this._selectItem(hit);
                }
                this._renderTimeline();

                const count = this.selectedItems.length;
                const itemLocked = (hit.type === "clip" && this._isLaneLocked(TRACK_TYPE.VIDEO, hit.data.track_index || 0))
                    || (hit.type === "audio" && this._isLaneLocked(TRACK_TYPE.AUDIO, hit.data.lane_index || 0));
                if (count > 1) {
                    menuItems.push({ label: `Delete ${count} items`, action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                } else if (hit.type === "clip") {
                    menuItems.push({ label: itemLocked ? "Move to New Lane (locked)" : "Move to New Lane", action: itemLocked ? () => {} : () => this._moveItemToNewLane(hit), disabled: itemLocked });
                    menuItems.push({ label: itemLocked ? "Delete Clip (locked)" : "Delete Clip", action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                } else if (hit.type === "audio") {
                    menuItems.push({ label: itemLocked ? "Move to New Lane (locked)" : "Move to New Lane", action: itemLocked ? () => {} : () => this._moveItemToNewLane(hit), disabled: itemLocked });
                    menuItems.push({ label: itemLocked ? "Delete Audio (locked)" : "Delete Audio Track", action: itemLocked ? () => {} : () => this._deleteSelectedItems(), danger: true, disabled: itemLocked });
                } else if (hit.type === "guide") {
                    menuItems.push({ label: "Delete Guide", action: () => this._deleteSelectedItems(), danger: true });
                }
            }

            // Check prompt track
            const _pli3 = this._promptLayoutIdx();
            const promptTrackY = _pli3 >= 0 ? this._trackY(_pli3) : -1;
            if (_pli3 >= 0 && y >= promptTrackY && y <= promptTrackY + this._trackH(_pli3)) {
                const sections = this.activeScene?.prompt_sections || [];
                const idx = sections.findIndex(s => frame >= s.start_frame && frame <= s.end_frame);
                if (idx >= 0) {
                    menuItems.push({ label: "Edit Prompt", action: () => {
                        this._selectedPromptIdx = idx;
                        this._showPromptEditor(sections[idx], idx);
                        this._renderTimeline();
                    }});
                    menuItems.push({ label: "Delete Prompt", action: () => {
                        if (confirm("Delete this prompt section?")) this._deletePromptSection(idx);
                    }, danger: true });
                }
            }

            if (menuItems.length > 0) {
                this._showContextMenu(e.clientX, e.clientY, menuItems);
            }
        });
    }

    async _handleAssetDrop(asset, frame, trackY) {
        if (!this.activeScene || !this.projectDir) return;
        this._pushUndo("add asset");
        const dirName = this.projectDir.split(/[/\\]/).pop();

        // Determine drop target lane from Y position
        let targetVideoLane = 0;
        let targetAudioLane = 0;
        if (trackY !== undefined) {
            for (let i = 0; i < this._trackLayout.length; i++) {
                const entry = this._trackLayout[i];
                const ty = this._trackY(i);
                const th = this._trackH(i);
                if (trackY >= ty && trackY < ty + th) {
                    if (entry.type === TRACK_TYPE.VIDEO) targetVideoLane = entry.laneIndex;
                    if (entry.type === TRACK_TYPE.AUDIO) targetAudioLane = entry.laneIndex;
                    break;
                }
            }
        }

        // Block drop on locked lanes
        if (this._isLaneLocked(TRACK_TYPE.VIDEO, targetVideoLane) || this._isLaneLocked(TRACK_TYPE.AUDIO, targetAudioLane)) return;

        // Auto-add lane if target lane has overlapping items at the drop frame
        const _findAsset = (id) => {
            for (const type of ["video", "image", "audio"]) {
                const found = (this.assets[type] || []).find(a => a.asset_id === id);
                if (found) return found;
            }
            return null;
        };
        if (asset.asset_type === "video") {
            const assetObj = _findAsset(asset.asset_id);
            const dropDuration = assetObj ? Math.max(1, assetObj.frame_count || 1) : 30;
            const dropEnd = frame + dropDuration;
            const hasOverlap = (this.activeScene.clips || []).some(c =>
                (c.track_index || 0) === targetVideoLane &&
                c.timeline_start_frame < dropEnd && c.timeline_end_frame > frame
            );
            if (hasOverlap) {
                // Auto-add a new video lane and place clip there
                const newCount = (this.activeScene.video_lane_count || 1) + 1;
                targetVideoLane = newCount - 1; // highest lane = top
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ video_lane_count: newCount }),
                });
                this.activeScene.video_lane_count = newCount;
                this._buildTrackLayout();
            }
            // Also check audio overlap for dual video+audio drops (always check —
            // server attempts dual_drop regardless, and has_audio may not be set yet)
            {
                const fps = this.fps || 24;
                const audioDuration = dropDuration; // video duration = audio duration
                const audioDropEnd = frame + audioDuration;
                const hasAudioOverlap = (this.activeScene.audio_tracks || []).some(a =>
                    (a.lane_index || 0) === targetAudioLane &&
                    a.timeline_start_frame < audioDropEnd && a.timeline_end_frame > frame
                );
                if (hasAudioOverlap) {
                    const newAudioCount = (this.activeScene.audio_lane_count || 1) + 1;
                    targetAudioLane = newAudioCount - 1;
                    await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ audio_lane_count: newAudioCount }),
                    });
                    this.activeScene.audio_lane_count = newAudioCount;
                    this._buildTrackLayout();
                }
            }
        } else if (asset.asset_type === "audio") {
            const fps = this.fps || 24;
            const assetObj = _findAsset(asset.asset_id);
            const dropDuration = assetObj ? Math.max(1, Math.round((assetObj.duration_sec || 1) * fps)) : 30;
            const dropEnd = frame + dropDuration;
            const hasOverlap = (this.activeScene.audio_tracks || []).some(a =>
                (a.lane_index || 0) === targetAudioLane &&
                a.timeline_start_frame < dropEnd && a.timeline_end_frame > frame
            );
            if (hasOverlap) {
                const newCount = (this.activeScene.audio_lane_count || 1) + 1;
                targetAudioLane = newCount - 1;
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ audio_lane_count: newCount }),
                });
                this.activeScene.audio_lane_count = newCount;
                this._buildTrackLayout();
            }
        }

        let resp;
        try {
            if (asset.asset_type === "image") {
                // Images always create guide frames (regardless of which track they're dropped on)
                resp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/guides`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        frame_index: frame,
                        asset_id: asset.asset_id,
                        source: "asset",
                        strength: 1.0,
                    }),
                });
                if (!resp.ok) {
                    console.warn("[LTX Editor] Guide creation failed:", resp.status, await resp.text());
                    return;
                }
                console.log("[LTX Editor] Guide frame created at frame", frame);
            } else if (asset.asset_type === "video") {
                // Drop video = create clip on target video lane (+ audio track if video has audio)
                const clipBody = {
                    asset_id: asset.asset_id,
                    timeline_start_frame: frame,
                    track_index: targetVideoLane,
                    audio_lane_index: targetAudioLane,
                    dual_drop: true,  // Always attempt — server handles gracefully
                };
                resp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/clips`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(clipBody),
                });
                if (!resp.ok) {
                    console.warn("[LTX Editor] Clip creation failed:", resp.status, await resp.text());
                    return;
                }
            } else if (asset.asset_type === "audio") {
                // Drop audio = create audio track on target audio lane
                resp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/audio_tracks`), {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        asset_id: asset.asset_id,
                        timeline_start_frame: frame,
                        lane_index: targetAudioLane,
                    }),
                });
                if (!resp.ok) {
                    console.warn("[LTX Editor] Audio track creation failed:", resp.status, await resp.text());
                    return;
                }
            }

            // Refresh scene data
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to drop asset:", e);
        }
    }

    // ── Lane Management ────────────────────────────────────────────────
    async _moveItemToNewLane(hit) {
        if (!this.activeScene || !this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        this._pushUndo("move to new lane");

        try {
            if (hit.type === "clip") {
                // Add a new video lane and move clip there
                const newCount = (this.activeScene.video_lane_count || 1) + 1;
                const newLane = newCount - 1;
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ video_lane_count: newCount }),
                });
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/clips/${hit.id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ track_index: newLane }),
                });
            } else if (hit.type === "audio") {
                const newCount = (this.activeScene.audio_lane_count || 1) + 1;
                const newLane = newCount - 1;
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ audio_lane_count: newCount }),
                });
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${hit.id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ lane_index: newLane }),
                });
            }
            await this._fetchScenes();
            this._buildTrackLayout();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to move item to new lane:", e);
        }
    }

    /** Check if a lane is locked */
    _isLaneLocked(type, laneIndex) {
        const idx = type === TRACK_TYPE.VIDEO
            ? this._videoLaneLayoutIdx(laneIndex)
            : this._audioLaneLayoutIdx(laneIndex);
        return idx >= 0 && this._trackLayout[idx]?.locked;
    }

    /** Check if a lane is hidden */
    _isLaneHidden(type, laneIndex) {
        const idx = type === TRACK_TYPE.VIDEO
            ? this._videoLaneLayoutIdx(laneIndex)
            : this._audioLaneLayoutIdx(laneIndex);
        return idx >= 0 && this._trackLayout[idx]?.hidden;
    }

    /** Start inline rename for a lane header */
    _startLaneRename(layoutIdx) {
        const entry = this._trackLayout[layoutIdx];
        const canvas = this.timelineCanvas;
        const rect = canvas.getBoundingClientRect();
        const s = this._uiScale;
        const headerW = this.isFullscreen ? LABEL_WIDTH_FS : LABEL_WIDTH;
        const ty = this._trackY(layoutIdx);
        const th = this._trackH(layoutIdx);

        const input = document.createElement("input");
        input.type = "text";
        input.value = entry.customName || "";
        input.placeholder = entry.label;
        input.style.cssText = `
            position: fixed;
            left: ${rect.left + 2 * s}px;
            top: ${rect.top + ty * s + 1}px;
            width: ${(headerW - 4) * s}px;
            height: ${(th - 2) * s}px;
            font-size: ${10 * s}px;
            background: #333;
            color: #fff;
            border: 1px solid #5af;
            padding: 0 3px;
            z-index: 10001;
            outline: none;
            box-sizing: border-box;
        `;

        const finish = (save) => {
            if (save) {
                const newName = input.value.trim();
                entry.customName = newName;
                entry.label = newName || (entry.type === TRACK_TYPE.VIDEO
                    ? ((this.activeScene?.video_lane_count || 1) > 1 ? `V${entry.laneIndex + 1}` : "Video")
                    : ((this.activeScene?.audio_lane_count || 1) > 1 ? `A${entry.laneIndex + 1}` : "Audio"));
                this._saveLaneConfig();
                this._renderTimeline();
            }
            input.remove();
        };

        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") { e.preventDefault(); finish(true); }
            else if (e.key === "Escape") { e.preventDefault(); finish(false); }
            e.stopPropagation();
        });
        input.addEventListener("blur", () => finish(true));

        document.body.appendChild(input);
        input.focus();
        input.select();
    }

    /** Persist lane configs (lock/hide/name/color) to server from current _trackLayout */
    async _saveLaneConfig() {
        if (!this.activeScene || !this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const videoConfigs = [];
        const audioConfigs = [];
        for (const e of this._trackLayout) {
            if (e.type === TRACK_TYPE.VIDEO) {
                videoConfigs[e.laneIndex] = { name: e.customName || "", color: e.color || "", locked: e.locked, hidden: e.hidden };
            } else if (e.type === TRACK_TYPE.AUDIO) {
                audioConfigs[e.laneIndex] = { name: e.customName || "", color: e.color || "", locked: e.locked, hidden: e.hidden };
            }
        }
        // Fill any sparse gaps
        for (let i = 0; i < videoConfigs.length; i++) if (!videoConfigs[i]) videoConfigs[i] = { name: "", color: "", locked: false, hidden: false };
        for (let i = 0; i < audioConfigs.length; i++) if (!audioConfigs[i]) audioConfigs[i] = { name: "", color: "", locked: false, hidden: false };
        try {
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ video_lane_configs: videoConfigs, audio_lane_configs: audioConfigs }),
            });
            // Update local scene data
            if (this.activeScene) {
                this.activeScene.video_lane_configs = videoConfigs;
                this.activeScene.audio_lane_configs = audioConfigs;
            }
        } catch (e) {
            console.warn("[LTX Editor] Failed to save lane config:", e);
        }
    }

    async _addLane(trackType) {
        if (!this.activeScene || !this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const body = {};
        if (isVideo) {
            body.video_lane_count = (this.activeScene.video_lane_count || 1) + 1;
        } else {
            body.audio_lane_count = (this.activeScene.audio_lane_count || 1) + 1;
        }
        try {
            this._pushUndo("add lane");
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            await this._fetchScenes();
            this._buildTrackLayout();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to add lane:", e);
        }
    }

    async _removeLaneWithItems(trackType, laneIndex) {
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const label = isVideo ? "video" : "audio";
        const items = isVideo
            ? (this.activeScene?.clips || []).filter(c => (c.track_index || 0) === laneIndex)
            : (this.activeScene?.audio_tracks || []).filter(a => (a.lane_index || 0) === laneIndex);
        const targetLane = laneIndex > 0 ? laneIndex - 1 : 1;
        const currentCount = isVideo
            ? (this.activeScene?.video_lane_count || 1)
            : (this.activeScene?.audio_lane_count || 1);

        // If target lane would be the same (only 1 lane) or no valid target, delete items instead
        const willMove = currentCount > 1 && targetLane !== laneIndex;
        const msg = willMove
            ? `This ${label} lane has ${items.length} item(s). Move them to lane ${targetLane} and remove this lane?`
            : `This ${label} lane has ${items.length} item(s). Delete them and remove this lane?`;
        if (!confirm(msg)) return;

        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            if (willMove) {
                // Move items to adjacent lane
                for (const item of items) {
                    const id = isVideo ? item.clip_id : item.track_id;
                    const endpoint = isVideo ? "clips" : "audio_tracks";
                    const field = isVideo ? "track_index" : "lane_index";
                    await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/${endpoint}/${id}`), {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ [field]: targetLane }),
                    });
                }
            } else {
                // Delete items on this lane
                for (const item of items) {
                    const id = isVideo ? item.clip_id : item.track_id;
                    const endpoint = isVideo ? "clips" : "audio_tracks";
                    await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/${endpoint}/${id}`), {
                        method: "DELETE",
                    });
                }
            }
            await this._removeLane(trackType, laneIndex);
        } catch (e) {
            console.warn("[LTX Editor] Failed to remove lane with items:", e);
        }
    }

    async _removeLane(trackType, laneIndex) {
        if (!this.activeScene || !this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const isVideo = trackType === TRACK_TYPE.VIDEO;
        const currentCount = isVideo
            ? (this.activeScene.video_lane_count || 1)
            : (this.activeScene.audio_lane_count || 1);
        if (currentCount <= 1) return;

        // Shift items on higher lanes down
        const body = {};
        if (isVideo) {
            body.video_lane_count = currentCount - 1;
        } else {
            body.audio_lane_count = currentCount - 1;
        }
        try {
            this._pushUndo("remove lane");
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(body),
            });
            // Shift items on lanes above the removed one
            if (isVideo) {
                for (const clip of (this.activeScene.clips || [])) {
                    if ((clip.track_index || 0) > laneIndex) {
                        await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/clips/${clip.clip_id}`), {
                            method: "PUT",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ track_index: clip.track_index - 1 }),
                        });
                    }
                }
            } else {
                for (const track of (this.activeScene.audio_tracks || [])) {
                    if ((track.lane_index || 0) > laneIndex) {
                        await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/audio_tracks/${track.track_id}`), {
                            method: "PUT",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ lane_index: track.lane_index - 1 }),
                        });
                    }
                }
            }
            await this._fetchScenes();
            this._buildTrackLayout();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to remove lane:", e);
        }
    }

    // ── Prompt Section Management ─────────────────────────────────────
    async _createPromptSection(frame) {
        if (!this.activeScene || !this.projectDir) return;

        // Use current selection if it exists, otherwise fill entire timeline
        let startFrame, endFrame;
        if (this.selectionStart < this.selectionEnd) {
            startFrame = this.selectionStart;
            endFrame = this.selectionEnd;
        } else {
            startFrame = 0;
            endFrame = this.totalFrames;
        }

        // Show inline editor for the new prompt section
        this._showPromptCreator(startFrame, endFrame);
    }

    _showPromptCreator(startFrame, endFrame) {
        this._hidePromptEditor();

        const editor = document.createElement("div");
        editor.style.cssText = `
            display: flex; gap: 4px; padding: 4px 6px;
            background: #1a1a1a; border-top: 1px solid ${COLORS.promptBorder};
            align-items: center;
        `;

        const label = document.createElement("span");
        label.style.cssText = `font-size: 10px; color: ${COLORS.promptBorder}; white-space: nowrap;`;
        label.textContent = `New [${startFrame}-${endFrame}]:`;

        const input = document.createElement("input");
        input.type = "text";
        input.placeholder = "Enter prompt for this section...";
        input.style.cssText = `
            flex: 1; background: #2a2a2a; border: 1px solid #555; color: #ddd;
            padding: 3px 6px; font-size: 11px; border-radius: 3px;
        `;
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter" && input.value.trim()) {
                this._saveNewPromptSection(startFrame, endFrame, input.value.trim());
            } else if (e.key === "Escape") {
                this._hidePromptEditor();
            }
            e.stopPropagation();
        });

        const createBtn = this._makeBtn("Create", "Create prompt section");
        createBtn.addEventListener("click", () => {
            if (input.value.trim()) {
                this._saveNewPromptSection(startFrame, endFrame, input.value.trim());
            }
        });

        const cancelBtn = this._makeBtn("Cancel", "Cancel");
        cancelBtn.style.color = COLORS.textDim;
        cancelBtn.addEventListener("click", () => this._hidePromptEditor());

        editor.append(label, input, createBtn, cancelBtn);
        this.timelineCanvas.parentElement.insertBefore(editor, this.timelineCanvas.nextSibling);
        this._promptEditorEl = editor;
        this._refreshTimelineLayout();

        setTimeout(() => input.focus(), 50);
    }

    async _saveNewPromptSection(startFrame, endFrame, promptText) {
        if (!this.activeScene || !this.projectDir) return;
        this._pushUndo("add prompt");
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/prompt_sections`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    start_frame: startFrame,
                    end_frame: endFrame,
                    prompt: promptText,
                }),
            });
            this._hidePromptEditor();
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to create prompt section:", e);
        }
    }

    _showPromptEditor(section, idx) {
        this._hidePromptEditor();

        const editor = document.createElement("div");
        editor.style.cssText = `
            display: flex; gap: 4px; padding: 4px 6px;
            background: #1a1a1a; border-top: 1px solid ${COLORS.promptBorder};
            align-items: center;
        `;

        const label = document.createElement("span");
        label.style.cssText = `font-size: 10px; color: ${COLORS.promptBorder}; white-space: nowrap;`;
        label.textContent = `Prompt [${section.start_frame}-${section.end_frame}]:`;

        const input = document.createElement("input");
        input.type = "text";
        input.value = section.prompt;
        input.style.cssText = `
            flex: 1; background: #2a2a2a; border: 1px solid #555; color: #ddd;
            padding: 3px 6px; font-size: 11px; border-radius: 3px;
        `;
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                this._updatePromptSection(idx, { prompt: input.value });
                this._hidePromptEditor();
            } else if (e.key === "Escape") {
                this._hidePromptEditor();
                this._selectedPromptIdx = null;
                this._renderTimeline();
            }
            e.stopPropagation();
        });

        const saveBtn = this._makeBtn("Save", "Save prompt");
        saveBtn.addEventListener("click", () => {
            this._updatePromptSection(idx, { prompt: input.value });
            this._hidePromptEditor();
        });

        const deleteBtn = this._makeBtn("Delete", "Delete this prompt section");
        deleteBtn.style.color = "#f66";
        deleteBtn.addEventListener("click", () => {
            if (confirm(`Delete this prompt section?`)) {
                this._deletePromptSection(idx);
            }
        });

        editor.append(label, input, saveBtn, deleteBtn);
        // Insert after timeline canvas
        this.timelineCanvas.parentElement.insertBefore(editor, this.timelineCanvas.nextSibling);
        this._promptEditorEl = editor;
        this._refreshTimelineLayout();

        // Focus input
        setTimeout(() => input.focus(), 50);
    }

    _hidePromptEditor() {
        if (this._promptEditorEl) {
            this._promptEditorEl.remove();
            this._promptEditorEl = null;
            this._refreshTimelineLayout();
        }
    }

    async _updatePromptSection(idx, updates) {
        if (!this.activeScene || !this.projectDir) return;
        this._pushUndo("edit prompt");
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/prompt_sections/${idx}`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(updates),
            });
            await this._fetchScenes();
            this._selectedPromptIdx = null;
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to update prompt section:", e);
        }
    }

    async _deletePromptSection(idx) {
        if (!this.activeScene || !this.projectDir) return;
        this._pushUndo("delete prompt");
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${this.activeSceneId}/prompt_sections/${idx}`), {
                method: "DELETE",
            });
            this._selectedPromptIdx = null;
            this._hidePromptEditor();
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to delete prompt section:", e);
        }
    }

    // ── Item Properties Editor ──────────────────────────────────────────
    _showItemEditor() {
        this._hideItemEditor();
        if (!this.selectedItem) return;

        const { type, id, data } = this.selectedItem;

        const editor = document.createElement("div");
        editor.style.cssText = `
            display: flex; gap: 6px; padding: 4px 6px;
            background: #1a1a1a; border-top: 1px solid ${type === "clip" ? COLORS.clipSelected : type === "audio" ? COLORS.audioClipSelected : COLORS.guideSelected};
            align-items: center; flex-wrap: wrap;
        `;

        const typeLabel = document.createElement("span");
        typeLabel.style.cssText = `font-size: 10px; color: ${type === "clip" ? COLORS.clipSelected : type === "audio" ? COLORS.audioClipSelected : COLORS.guideSelected}; white-space: nowrap; font-weight: bold;`;
        typeLabel.textContent = type === "clip" ? "Video Clip" : type === "audio" ? "Audio Track" : "Guide Frame";
        editor.appendChild(typeLabel);

        if (type === "clip" || type === "audio") {
            const startFrame = data.timeline_start_frame;
            const endFrame = data.timeline_end_frame;
            const duration = endFrame - startFrame;

            // Start frame input
            const startLabel = this._makeEditorLabel("Start:");
            const startInput = this._makeEditorInput(startFrame, 0, this.totalFrames);
            editor.append(startLabel, startInput);

            // Duration display
            const durLabel = this._makeEditorLabel(`Duration: ${this._frameToTimecode(duration)}`);
            durLabel.style.color = COLORS.textDim;
            editor.appendChild(durLabel);

            // Opacity (clips) or Volume (audio)
            if (type === "clip") {
                const opLabel = this._makeEditorLabel("Opacity:");
                const opInput = document.createElement("input");
                opInput.type = "range";
                opInput.min = 0; opInput.max = 100; opInput.step = 5;
                opInput.value = Math.round((data.opacity ?? 1.0) * 100);
                opInput.style.cssText = `width: 60px; height: 14px; cursor: pointer; accent-color: ${COLORS.clip};`;
                opInput.title = `Opacity: ${opInput.value}%`;
                const opVal = this._makeEditorLabel(`${opInput.value}%`);
                opVal.style.minWidth = "28px";
                opInput.addEventListener("input", () => {
                    opVal.textContent = `${opInput.value}%`;
                    opInput.title = `Opacity: ${opInput.value}%`;
                    data.opacity = parseInt(opInput.value) / 100;
                    this._renderTimeline();
                });
                opInput.addEventListener("change", () => {
                    this._updateItemProperty(type, id, { opacity: parseInt(opInput.value) / 100 });
                });
                editor.append(opLabel, opInput, opVal);
            } else {
                // Volume slider for audio
                const volLabel = this._makeEditorLabel("Vol:");
                const volInput = document.createElement("input");
                volInput.type = "range";
                volInput.min = 0; volInput.max = 100; volInput.step = 5;
                volInput.value = Math.round((data.volume ?? 1.0) * 100);
                volInput.style.cssText = `width: 60px; height: 14px; cursor: pointer; accent-color: ${COLORS.audioClip};`;
                volInput.title = `Volume: ${volInput.value}%`;
                const volVal = this._makeEditorLabel(`${volInput.value}%`);
                volVal.style.minWidth = "28px";
                volInput.addEventListener("input", () => {
                    volVal.textContent = `${volInput.value}%`;
                    volInput.title = `Volume: ${volInput.value}%`;
                    data.volume = parseInt(volInput.value) / 100;
                    this._renderTimeline();
                });
                volInput.addEventListener("change", () => {
                    this._updateItemProperty(type, id, { volume: parseInt(volInput.value) / 100 });
                });

                // Mute toggle
                const muteBtn = this._makeBtn(data.muted ? "🔇" : "🔊", "Toggle mute");
                muteBtn.addEventListener("click", () => {
                    this._pushUndo("toggle mute");
                    data.muted = !data.muted;
                    muteBtn.textContent = data.muted ? "🔇" : "🔊";
                    this._updateItemProperty(type, id, { muted: data.muted });
                    this._renderTimeline();
                });
                editor.append(volLabel, volInput, volVal, muteBtn);
            }

            // Apply button
            const applyBtn = this._makeBtn("Apply", "Apply position change");
            applyBtn.addEventListener("click", () => {
                const newStart = parseInt(startInput.value, 10);
                if (!isNaN(newStart) && newStart >= 0) {
                    this._moveItemToFrame(type, id, data, newStart);
                }
            });
            editor.appendChild(applyBtn);

            // Enter key in input
            startInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    const newStart = parseInt(startInput.value, 10);
                    if (!isNaN(newStart) && newStart >= 0) {
                        this._moveItemToFrame(type, id, data, newStart);
                    }
                } else if (e.key === "Escape") {
                    this._hideItemEditor();
                    this._clearSelection();
                    this._renderTimeline();
                }
                e.stopPropagation();
            });

        } else if (type === "guide") {
            let idx = data.frame_index;
            if (idx === -1) idx = this.totalFrames - 1;

            // Frame index input
            const frameLabel = this._makeEditorLabel("Frame:");
            const frameInput = this._makeEditorInput(idx, 0, this.totalFrames - 1);
            editor.append(frameLabel, frameInput);

            // Strength display
            const strengthLabel = this._makeEditorLabel(`Strength: ${(data.strength ?? 1.0).toFixed(2)}`);
            strengthLabel.style.color = COLORS.textDim;
            editor.appendChild(strengthLabel);

            // Apply button
            const applyBtn = this._makeBtn("Apply", "Apply position change");
            applyBtn.addEventListener("click", () => {
                const newIdx = parseInt(frameInput.value, 10);
                if (!isNaN(newIdx) && newIdx >= 0 && newIdx !== data.frame_index) {
                    this._moveGuideToFrame(data, newIdx);
                }
            });
            editor.appendChild(applyBtn);

            // Enter key in input
            frameInput.addEventListener("keydown", (e) => {
                if (e.key === "Enter") {
                    const newIdx = parseInt(frameInput.value, 10);
                    if (!isNaN(newIdx) && newIdx >= 0 && newIdx !== data.frame_index) {
                        this._moveGuideToFrame(data, newIdx);
                    }
                } else if (e.key === "Escape") {
                    this._hideItemEditor();
                    this._clearSelection();
                    this._renderTimeline();
                }
                e.stopPropagation();
            });
        }

        // Delete button (always present)
        const deleteBtn = this._makeBtn("Delete", "Delete this item");
        deleteBtn.style.color = "#f66";
        deleteBtn.addEventListener("click", () => this._deleteSelectedItems());
        editor.appendChild(deleteBtn);

        // Insert after timeline canvas
        this.timelineCanvas.parentElement.insertBefore(editor, this.timelineCanvas.nextSibling);
        this._itemEditorEl = editor;
        this._refreshTimelineLayout();
    }

    _hideItemEditor() {
        if (this._itemEditorEl) {
            this._itemEditorEl.remove();
            this._itemEditorEl = null;
            this._refreshTimelineLayout();
        }
    }

    _refreshTimelineLayout() {
        if (this.isFullscreen) {
            this._recalcFullscreenHeights();
        }
        this._renderTimeline();
    }

    _makeEditorLabel(text) {
        const label = document.createElement("span");
        label.style.cssText = `font-size: 10px; color: ${COLORS.text}; white-space: nowrap;`;
        label.textContent = text;
        return label;
    }

    _makeEditorInput(value, min, max) {
        const input = document.createElement("input");
        input.type = "number";
        input.value = value;
        input.min = min;
        input.max = max;
        input.style.cssText = `
            width: 60px; background: #2a2a2a; border: 1px solid #555; color: #ddd;
            padding: 2px 4px; font-size: 11px; border-radius: 3px; text-align: right;
        `;
        return input;
    }

    async _moveItemToFrame(type, id, data, newStart) {
        if (!this.activeScene || !this.projectDir) return;
        this._pushUndo("move item");
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        const endpoint = type === "clip"
            ? `/ltx-editor/project/${dirName}/scenes/${sceneId}/clips/${id}`
            : `/ltx-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${id}`;

        try {
            await fetch(api.apiURL(endpoint), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ timeline_start_frame: newStart }),
            });
            await this._fetchScenes();
            this._clearSelection();
            this._hideItemEditor();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to move item:", e);
        }
    }

    async _updateItemProperty(type, id, props) {
        if (!this.activeScene || !this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        const endpoint = type === "clip"
            ? `/ltx-editor/project/${dirName}/scenes/${sceneId}/clips/${id}`
            : `/ltx-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${id}`;

        try {
            await fetch(api.apiURL(endpoint), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(props),
            });
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to update item property:", e);
        }
    }

    async _moveGuideToFrame(guideData, newIdx) {
        if (!this.activeScene || !this.projectDir) return;
        this._pushUndo("move guide");
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        const oldIdx = guideData.frame_index;

        try {
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/guides/${oldIdx}`), {
                method: "DELETE",
            });
            await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/guides`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    frame_index: newIdx,
                    asset_id: guideData.asset_id,
                    source: guideData.source || "asset",
                    strength: guideData.strength ?? 1.0,
                }),
            });
            await this._fetchScenes();
            this._clearSelection();
            this._hideItemEditor();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to move guide:", e);
        }
    }

    // ── Item Delete / Move ──────────────────────────────────────────────
    async _deleteSelectedItems() {
        if (this.selectedItems.length === 0 || !this.activeScene || !this.projectDir) return;
        this._pushUndo("delete items");
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;

        try {
            for (const item of this.selectedItems) {
                let endpoint;
                if (item.type === "clip") {
                    endpoint = `/ltx-editor/project/${dirName}/scenes/${sceneId}/clips/${item.id}`;
                } else if (item.type === "audio") {
                    endpoint = `/ltx-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${item.id}`;
                } else if (item.type === "guide") {
                    endpoint = `/ltx-editor/project/${dirName}/scenes/${sceneId}/guides/${item.id}`;
                } else if (item.type === "prompt") {
                    endpoint = `/ltx-editor/project/${dirName}/scenes/${sceneId}/prompt_sections/${item.id}`;
                }
                if (endpoint) {
                    await fetch(api.apiURL(endpoint), { method: "DELETE" });
                }
            }
            this._clearSelection();
            this._hideItemEditor();
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to delete items:", e);
        }
    }

    async _commitItemMove(frameDelta) {
        if (this.selectedItems.length === 0 || !this.activeScene || !this.projectDir) return;
        // Note: _pushUndo already called at drag start (mousedown)
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;

        try {
            // Lane swap: use original lane snapshots to find items that need swapping
            const draggedClipIds = new Set((this._dragItemsOrig || []).filter(o => o.type === "clip").map(o => o.id));
            const draggedAudioIds = new Set((this._dragItemsOrig || []).filter(o => o.type === "audio").map(o => o.id));
            const origClipLanes = this._origAllClipLanes || {};
            const origAudioLanes = this._origAllAudioLanes || {};

            for (const orig of (this._dragItemsOrig || [])) {
                if (orig.type === "clip") {
                    const newLane = orig.data.track_index || 0;
                    if (newLane !== orig.origLane) {
                        // Swap: all non-dragged clips originally on targetLane → origLane
                        for (const c of (this.activeScene.clips || [])) {
                            if (!draggedClipIds.has(c.clip_id) && (origClipLanes[c.clip_id] ?? 0) === newLane) {
                                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/clips/${c.clip_id}`), {
                                    method: "PUT",
                                    headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify({ track_index: orig.origLane }),
                                });
                            }
                        }
                    }
                } else if (orig.type === "audio") {
                    const newLane = orig.data.lane_index || 0;
                    if (newLane !== orig.origLane) {
                        for (const a of (this.activeScene.audio_tracks || [])) {
                            if (!draggedAudioIds.has(a.track_id) && (origAudioLanes[a.track_id] ?? 0) === newLane) {
                                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${a.track_id}`), {
                                    method: "PUT",
                                    headers: { "Content-Type": "application/json" },
                                    body: JSON.stringify({ lane_index: orig.origLane }),
                                });
                            }
                        }
                    }
                }
            }

            // Now commit the dragged items themselves
            for (const orig of (this._dragItemsOrig || [])) {
                const { type, id, data } = orig;
                if (type === "clip") {
                    const newStart = Math.max(0, orig.origStart + frameDelta);
                    const putBody = { timeline_start_frame: newStart };
                    if (data.track_index !== undefined) putBody.track_index = data.track_index;
                    await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/clips/${id}`), {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(putBody),
                    });
                } else if (type === "audio") {
                    const newStart = Math.max(0, orig.origStart + frameDelta);
                    const putBody = { timeline_start_frame: newStart };
                    if (data.lane_index !== undefined) putBody.lane_index = data.lane_index;
                    await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${id}`), {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify(putBody),
                    });
                } else if (type === "guide") {
                    const oldIdx = orig.origStart;
                    const newIdx = Math.max(0, Math.min(this.totalFrames - 1, oldIdx + frameDelta));
                    // Move guide = delete old + create new
                    await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/guides/${oldIdx}`), {
                        method: "DELETE",
                    });
                    await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/guides`), {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            frame_index: newIdx,
                            asset_id: data.asset_id,
                            source: data.source || "asset",
                            strength: data.strength ?? 1.0,
                        }),
                    });
                    delete data._previewFrameIndex;
                } else if (type === "prompt") {
                    const duration = orig.origEnd - orig.origStart;
                    const newStart = Math.max(0, orig.origStart + frameDelta);
                    await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/prompt_sections/${id}`), {
                        method: "PUT",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            start_frame: newStart,
                            end_frame: newStart + duration,
                        }),
                    });
                }
            }

            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to move items:", e);
            await this._fetchScenes();
            this._renderTimeline();
        }
    }

    /** Commit a trim operation (edge drag) to the server. */
    async _commitTrim(trimInfo) {
        if (!this.projectDir || !this.activeScene) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;
        const { type, id, data } = trimInfo;

        try {
            if (type === "clip") {
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/clips/${id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        timeline_start_frame: data.timeline_start_frame,
                        timeline_end_frame: data.timeline_end_frame,
                        source_in_frame: data.source_in_frame || 0,
                        source_out_frame: data.source_out_frame,
                    }),
                });
            } else if (type === "audio") {
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        timeline_start_frame: data.timeline_start_frame,
                        timeline_end_frame: data.timeline_end_frame,
                        source_in_frame: data.source_in_frame || 0,
                    }),
                });
            } else if (type === "prompt") {
                await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/prompt_sections/${id}`), {
                    method: "PUT",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        start_frame: data.start_frame,
                        end_frame: data.end_frame,
                    }),
                });
            }
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn("[LTX Editor] Failed to commit trim:", e);
            await this._fetchScenes();
            this._renderTimeline();
        }
    }

    /** Split a clip at the given frame (razor tool). */
    async _splitClipAtFrame(hit, frame) {
        if (!this.projectDir || !this.activeScene) return;
        if (hit.type !== "clip" && hit.type !== "audio") return;
        if (frame <= hit.data.timeline_start_frame || frame >= hit.data.timeline_end_frame) return;
        // Block split on locked lanes
        if (hit.type === "clip" && this._isLaneLocked(TRACK_TYPE.VIDEO, hit.data.track_index || 0)) return;
        if (hit.type === "audio" && this._isLaneLocked(TRACK_TYPE.AUDIO, hit.data.lane_index || 0)) return;

        this._pushUndo(`split ${hit.type}`);
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const sceneId = this.activeSceneId;

        try {
            const endpoint = hit.type === "clip"
                ? `/ltx-editor/project/${dirName}/scenes/${sceneId}/clips/${hit.id}/split`
                : `/ltx-editor/project/${dirName}/scenes/${sceneId}/audio_tracks/${hit.id}/split`;
            await fetch(api.apiURL(endpoint), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ frame }),
            });
            await this._fetchScenes();
            this._renderTimeline();
        } catch (e) {
            console.warn(`[LTX Editor] Failed to split ${hit.type}:`, e);
        }
    }

    // ── Context Menu ──────────────────────────────────────────────────
    _showContextMenu(x, y, items) {
        this._hideContextMenu();

        const menu = document.createElement("div");
        menu.style.cssText = `
            position: fixed; left: ${x}px; top: ${y}px; z-index: 10000;
            background: #2a2a2a; border: 1px solid #555; border-radius: 4px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.5); min-width: 140px;
            padding: 4px 0; font-size: 11px;
        `;

        for (const item of items) {
            const row = document.createElement("div");
            row.textContent = item.label;
            const isDisabled = item.disabled;
            row.style.cssText = `
                padding: 6px 14px; cursor: ${isDisabled ? "default" : "pointer"};
                color: ${isDisabled ? "#666" : (item.danger ? "#f66" : "#ddd")};
            `;
            if (!isDisabled) {
                row.addEventListener("mouseenter", () => row.style.background = "#3a3a3a");
                row.addEventListener("mouseleave", () => row.style.background = "transparent");
                row.addEventListener("click", () => {
                    this._hideContextMenu();
                    item.action();
                });
            }
            menu.appendChild(row);
        }

        document.body.appendChild(menu);
        this._contextMenuEl = menu;

        // Close on outside click or Escape
        const closeHandler = (e) => {
            if (!menu.contains(e.target)) {
                this._hideContextMenu();
                document.removeEventListener("mousedown", closeHandler);
                document.removeEventListener("keydown", escHandler);
            }
        };
        const escHandler = (e) => {
            if (e.key === "Escape") {
                this._hideContextMenu();
                document.removeEventListener("mousedown", closeHandler);
                document.removeEventListener("keydown", escHandler);
            }
        };
        // Delay listener registration to avoid catching the current right-click
        setTimeout(() => {
            document.addEventListener("mousedown", closeHandler);
            document.addEventListener("keydown", escHandler);
        }, 10);
    }

    _hideContextMenu() {
        if (this._contextMenuEl) {
            this._contextMenuEl.remove();
            this._contextMenuEl = null;
        }
    }

    // ── Keyboard Shortcut Overlay ────────────────────────────────────
    _showShortcutOverlay() {
        if (this._shortcutOverlayEl) return;
        const backdrop = document.createElement("div");
        backdrop.style.cssText = `position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,0.7);display:flex;align-items:center;justify-content:center;`;
        const panel = document.createElement("div");
        panel.style.cssText = `background:#1e1e1e;border:1px solid #555;border-radius:8px;padding:20px 28px;max-width:520px;width:90%;max-height:80vh;overflow-y:auto;color:#ddd;font-family:monospace;font-size:12px;`;
        panel.innerHTML = `<h3 style="margin:0 0 12px;color:#fff;font-size:14px;">Keyboard Shortcuts</h3>` +
            this._shortcutSection("Playback", [
                ["Space", "Play / Pause (fullscreen)"],
                ["\u2190 / \u2192", "Frame back / forward"],
                ["Shift+\u2190 / \u2192", "10 frames back / forward"],
                ["Home / End", "Go to first / last frame"],
            ]) +
            this._shortcutSection("Selection", [
                ["I", "Set in-point"],
                ["O", "Set out-point"],
                ["X", "Clear selection"],
            ]) +
            this._shortcutSection("Tools", [
                ["C", "Toggle razor / cut mode"],
                ["S", "Toggle snapping"],
                ["T", "Toggle timecode display"],
                ["F", "Fit timeline to view"],
                ["Shift+F", "Zoom to selection"],
            ]) +
            this._shortcutSection("Edit", [
                ["Del / Backspace", "Delete selected items"],
                ["Ctrl+Z", "Undo"],
                ["Ctrl+Y", "Redo"],
            ]) +
            this._shortcutSection("View", [
                ["+ / -", "Zoom in / out"],
                ["Esc", "Exit fullscreen"],
                ["?", "Show this overlay"],
            ]);

        backdrop.appendChild(panel);
        document.body.appendChild(backdrop);
        this._shortcutOverlayEl = backdrop;

        backdrop.addEventListener("click", (e) => { if (e.target === backdrop) this._hideShortcutOverlay(); });
        this._shortcutOverlayEscHandler = (e) => { if (e.key === "Escape") { e.stopImmediatePropagation(); this._hideShortcutOverlay(); } };
        document.addEventListener("keydown", this._shortcutOverlayEscHandler, true);
    }

    _shortcutSection(title, shortcuts) {
        let html = `<div style="margin-bottom:10px;"><div style="color:#aaa;font-size:11px;margin-bottom:4px;text-transform:uppercase;">${title}</div>`;
        for (const [key, desc] of shortcuts) {
            html += `<div style="display:flex;justify-content:space-between;padding:2px 0;"><span style="color:#6cf;min-width:120px;">${key}</span><span>${desc}</span></div>`;
        }
        return html + `</div>`;
    }

    _hideShortcutOverlay() {
        if (this._shortcutOverlayEl) {
            this._shortcutOverlayEl.remove();
            this._shortcutOverlayEl = null;
        }
        if (this._shortcutOverlayEscHandler) {
            document.removeEventListener("keydown", this._shortcutOverlayEscHandler, true);
            this._shortcutOverlayEscHandler = null;
        }
    }

    // ── Fullscreen Mode ──────────────────────────────────────────────
    _createFullscreenOverlay() {
        if (this._fullscreenOverlay) return;

        const overlay = document.createElement("div");
        overlay.style.cssText = `
            position: fixed; inset: 0; z-index: 9999;
            background: #111; display: none;
            flex-direction: column;
        `;

        // Toolbar
        const toolbar = document.createElement("div");
        toolbar.style.cssText = `
            display: flex; align-items: center; padding: 0 12px;
            height: 40px; background: #1a1a1a; border-bottom: 1px solid #333;
            flex-shrink: 0;
        `;

        this._fsTitle = document.createElement("span");
        this._fsTitle.style.cssText = `font-size: 13px; color: ${COLORS.text}; font-weight: 600;`;
        this._fsTitle.textContent = "LTX Editor";

        const spacer = document.createElement("span");
        spacer.style.flex = "1";

        const exitBtn = this._makeBtn("✕ Exit", "Exit fullscreen");
        exitBtn.style.cssText += `font-size: 12px; padding: 4px 12px; color: ${COLORS.textDim};`;
        exitBtn.addEventListener("click", () => this._exitFullscreen());

        toolbar.append(this._fsTitle, spacer, exitBtn);

        // Content area — three-panel layout
        this._fsContent = document.createElement("div");
        this._fsContent.style.cssText = `
            flex: 1; display: flex; flex-direction: column; overflow: hidden;
        `;

        // Top row: assets sidebar + viewport
        this._fsTopRow = document.createElement("div");
        this._fsTopRow.style.cssText = `
            flex: 1; display: flex; overflow: hidden; min-height: 0;
        `;

        // Assets sidebar (left)
        this._fsSidebar = document.createElement("div");
        this._fsSidebar.style.cssText = `
            width: 240px; min-width: 180px; max-width: 400px;
            background: ${COLORS.galleryBg}; border-right: 2px solid #333;
            display: flex; flex-direction: column; overflow: hidden;
            flex-shrink: 0; position: relative;
        `;

        // Sidebar header with project name
        this._fsSidebarHeader = document.createElement("div");
        this._fsSidebarHeader.style.cssText = `
            padding: 8px 12px; background: #1a1a1a; border-bottom: 1px solid #333;
            font-size: 12px; color: ${COLORS.text}; font-weight: 600;
            flex-shrink: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
        `;
        this._fsSidebarHeader.textContent = "Assets";
        this._fsSidebar.appendChild(this._fsSidebarHeader);

        // Sidebar resize handle
        const sidebarHandle = document.createElement("div");
        sidebarHandle.style.cssText = `
            position: absolute; right: -3px; top: 0; bottom: 0; width: 6px;
            cursor: ew-resize; z-index: 2;
        `;
        this._setupResizeHandle(sidebarHandle, this._fsSidebar, "width", 180, 400);
        this._fsSidebar.appendChild(sidebarHandle);

        // Viewport panel (center)
        this._fsViewport = document.createElement("div");
        this._fsViewport.style.cssText = `
            flex: 1; display: flex; flex-direction: column;
            background: #0a0a0a; overflow: hidden; min-width: 0;
        `;

        // Viewport header
        const vpHeader = document.createElement("div");
        vpHeader.style.cssText = `
            padding: 6px 12px; background: #1a1a1a; border-bottom: 1px solid #333;
            font-size: 11px; color: ${COLORS.textDim}; flex-shrink: 0;
            display: flex; justify-content: space-between; align-items: center;
        `;
        this._vpHeaderText = document.createElement("span");
        this._vpHeaderText.textContent = `${this.sceneWidth}×${this.sceneHeight} @ ${this.fps}fps`;
        vpHeader.appendChild(this._vpHeaderText);

        // Viewport content area — canvas for video preview
        this._fsViewportContent = document.createElement("div");
        this._fsViewportContent.style.cssText = `
            flex: 1; display: flex; align-items: center; justify-content: center;
            min-height: 0; position: relative; overflow: hidden;
        `;

        // Viewport canvas
        this._vpCanvas = document.createElement("canvas");
        this._vpCanvas.style.cssText = `display: block; background: #000;`;
        this._vpCtx = this._vpCanvas.getContext("2d");
        this._fsViewportContent.appendChild(this._vpCanvas);

        // Viewport frame overlay (shows frame number when no clip)
        this._vpFrameLabel = document.createElement("div");
        this._vpFrameLabel.style.cssText = `
            position: absolute; bottom: 8px; right: 8px;
            font-size: 11px; color: rgba(255,255,255,0.5);
            pointer-events: none; font-family: monospace;
        `;
        this._fsViewportContent.appendChild(this._vpFrameLabel);

        // ResizeObserver to recalc viewport canvas dimensions
        this._vpResizeObserver = new ResizeObserver(() => this._resizeViewportCanvas());
        this._vpResizeObserver.observe(this._fsViewportContent);

        // Transport bar
        const transport = document.createElement("div");
        transport.style.cssText = `
            height: 36px; flex-shrink: 0; background: #1a1a1a;
            border-top: 1px solid #333; display: flex; align-items: center;
            padding: 0 12px; gap: 10px;
        `;

        // Play/Pause button
        this._vpPlayBtn = document.createElement("button");
        this._vpPlayBtn.textContent = "▶";
        this._vpPlayBtn.style.cssText = `
            background: none; border: 1px solid #555; color: #ddd;
            cursor: pointer; padding: 4px 10px; border-radius: 3px;
            font-size: 12px; min-width: 32px;
        `;
        this._vpPlayBtn.addEventListener("click", () => this._togglePlayback());

        // Frame counter
        this._vpFrameCounter = document.createElement("span");
        this._vpFrameCounter.style.cssText = `
            font-size: 11px; color: ${COLORS.text}; font-family: monospace;
        `;
        this._vpFrameCounter.textContent = "Frame 0 / 0";

        // Progress bar
        const progressWrap = document.createElement("div");
        progressWrap.style.cssText = `
            flex: 1; height: 6px; background: #333; border-radius: 3px;
            cursor: pointer; position: relative;
        `;
        this._vpProgressFill = document.createElement("div");
        this._vpProgressFill.style.cssText = `
            height: 100%; background: ${COLORS.selection}; border-radius: 3px;
            width: 0%; pointer-events: none;
        `;
        progressWrap.appendChild(this._vpProgressFill);
        progressWrap.addEventListener("click", (e) => {
            const rect = progressWrap.getBoundingClientRect();
            const pct = (e.clientX - rect.left) / rect.width;
            this.playhead = Math.round(pct * this.totalFrames);
            this._renderTimeline();
            this._renderViewportFrame();
        });

        transport.append(this._vpPlayBtn, this._vpFrameCounter, progressWrap);
        this._fsViewport.append(vpHeader, this._fsViewportContent, transport);
        this._fsTopRow.append(this._fsSidebar, this._fsViewport);

        // Bottom area: timeline (will hold this.container minus gallery)
        this._fsBottomRow = document.createElement("div");
        const _defaultTimelineH = Math.max(200, Math.min(600, Math.round(window.innerHeight * 0.4)));
        this._fsBottomRow.style.cssText = `
            height: ${_defaultTimelineH}px; min-height: 160px; max-height: 600px;
            border-top: 2px solid #333; display: flex; flex-direction: column;
            overflow: hidden; flex-shrink: 0; position: relative;
        `;

        // Timeline resize handle (top edge)
        const timelineHandle = document.createElement("div");
        timelineHandle.style.cssText = `
            position: absolute; top: -3px; left: 0; right: 0; height: 6px;
            cursor: ns-resize; z-index: 2;
        `;
        this._setupResizeHandle(timelineHandle, this._fsBottomRow, "height", 160, 600, true);
        this._fsBottomRow.appendChild(timelineHandle);

        this._fsContent.append(this._fsTopRow, this._fsBottomRow);
        overlay.append(toolbar, this._fsContent);
        document.body.appendChild(overlay);
        this._fullscreenOverlay = overlay;
    }

    _setupResizeHandle(handle, target, prop, min, max, invert = false) {
        let startPos = 0;
        let startSize = 0;

        const onMouseMove = (e) => {
            const delta = prop === "width"
                ? e.clientX - startPos
                : e.clientY - startPos;
            const newSize = invert
                ? Math.max(min, Math.min(max, startSize - delta))
                : Math.max(min, Math.min(max, startSize + delta));
            target.style[prop] = newSize + "px";

            // Recalc timeline height to fill remaining space
            if (this.isFullscreen) {
                this._recalcFullscreenHeights();
                this._renderTimeline();
            }
        };

        const onMouseUp = () => {
            document.removeEventListener("mousemove", onMouseMove);
            document.removeEventListener("mouseup", onMouseUp);
            document.body.style.cursor = "";
            document.body.style.userSelect = "";
        };

        handle.addEventListener("mousedown", (e) => {
            e.preventDefault();
            startPos = prop === "width" ? e.clientX : e.clientY;
            startSize = parseInt(getComputedStyle(target)[prop]);
            document.body.style.cursor = prop === "width" ? "ew-resize" : "ns-resize";
            document.body.style.userSelect = "none";
            document.addEventListener("mousemove", onMouseMove);
            document.addEventListener("mouseup", onMouseUp);
        });
    }

    _recalcFullscreenHeights() {
        // In three-panel layout, timeline height is based on the bottom row height
        const bottomH = this._fsBottomRow ? parseInt(getComputedStyle(this._fsBottomRow).height) || 280 : 280;
        const s = this._uiScale;
        const sceneBarH = SCENE_BAR_HEIGHT * s;
        const toolbarH = 24 * s;
        const infoBarH = 24 * s;
        const editorsH = ((this._promptEditorEl ? 30 : 0) + (this._itemEditorEl ? 30 : 0)) * s;

        // Timeline logical height (canvas will multiply by s when rendering)
        this._timelineHeight = Math.max(100, (bottomH - sceneBarH - toolbarH - infoBarH - editorsH) / s);
        // Gallery is in the sidebar now, doesn't need height calc
        this._galleryHeight = GALLERY_HEIGHT; // Not used in fullscreen layout
    }

    _enterFullscreen() {
        // Module-level guard: only one fullscreen at a time
        if (EditorWidget._activeFullscreen && EditorWidget._activeFullscreen !== this) return;

        this._createFullscreenOverlay();

        // Save position and node size for re-insertion
        this._nodeParent = this.container.parentElement;
        this._nodeSibling = this.container.nextSibling;
        this._savedNodeSize = this.node.size ? [...this.node.size] : null;

        // Reparent: gallery goes to sidebar, rest of container goes to bottom row
        // Save gallery's position in container for restoration
        this._galleryNextSibling = this.galleryEl.nextSibling;

        // Update sidebar header
        const dirName = this.projectDir ? this.projectDir.split(/[/\\]/).pop() : "Assets";
        if (this._fsSidebarHeader) this._fsSidebarHeader.textContent = dirName;

        // Move gallery to sidebar
        this._fsSidebar.appendChild(this.galleryEl);
        this.galleryEl.style.flex = "1";
        this.galleryEl.style.minHeight = "0";
        this.galleryEl.style.overflow = "hidden";
        this.galleryEl.style.display = "flex";
        this.galleryEl.style.flexDirection = "column";
        this.assetGrid.style.maxHeight = "none";
        this.assetGrid.style.flex = "1";
        this.assetGrid.style.overflowY = "auto";
        this.assetGrid.style.minHeight = "0";
        this.assetGrid.style.gridTemplateColumns = "repeat(auto-fill, minmax(70px, 1fr))";

        // Move timeline container (without gallery) to bottom row
        this._fsBottomRow.appendChild(this.container);
        this.container.style.flex = "1";
        this.container.style.overflow = "hidden";

        // Show overlay
        this._fullscreenOverlay.style.display = "flex";

        // Update toolbar title
        this._fsTitle.textContent = `Editor — ${this.activeScene?.name || "No Scene"}`;

        // Insert placeholder into node
        this._fullscreenPlaceholder = document.createElement("div");
        this._fullscreenPlaceholder.style.cssText = `
            text-align: center; padding: 16px; color: ${COLORS.textDim};
            background: #1a1a1a; font-size: 12px;
        `;
        this._fullscreenPlaceholder.innerHTML = `Editor is in fullscreen mode`;
        const exitPlaceholderBtn = this._makeBtn("Exit Fullscreen", "Return editor to node");
        exitPlaceholderBtn.style.cssText += `display: block; margin: 8px auto 0;`;
        exitPlaceholderBtn.addEventListener("click", () => this._exitFullscreen());
        this._fullscreenPlaceholder.appendChild(exitPlaceholderBtn);

        if (this._nodeParent) {
            this._nodeParent.insertBefore(this._fullscreenPlaceholder, this._nodeSibling);
        }

        // Set fullscreen state + recalc
        this.isFullscreen = true;
        EditorWidget._activeFullscreen = this;
        this._fullscreenBtn.textContent = "⛶";
        this._fullscreenBtn.title = "Exit fullscreen";

        this._recalcFullscreenHeights();
        this._renderTimeline();

        // Render viewport after layout settles
        requestAnimationFrame(() => {
            this._resizeViewportCanvas();
            this._renderViewportFrame();
        });

        // Collapse node
        this.node.setSize?.(this.node.computeSize?.());
    }

    _exitFullscreen() {
        if (!this.isFullscreen) return;

        // Stop playback before exiting
        this._stopPlayback();

        // Remove placeholder
        if (this._fullscreenPlaceholder) {
            this._fullscreenPlaceholder.remove();
            this._fullscreenPlaceholder = null;
        }

        // Move gallery back into container (before its original next sibling)
        if (this.galleryEl) {
            this.galleryEl.style.flex = "";
            this.galleryEl.style.minHeight = GALLERY_HEIGHT + "px";
            this.galleryEl.style.overflow = "";
            this.galleryEl.style.display = "";
            this.galleryEl.style.flexDirection = "";
            this.assetGrid.style.maxHeight = (GALLERY_HEIGHT - 30) + "px";
            this.assetGrid.style.flex = "";
            this.assetGrid.style.overflowY = "auto";
            this.assetGrid.style.minHeight = "60px";
            this.assetGrid.style.gridTemplateColumns = "";
            this.container.insertBefore(this.galleryEl, this._galleryNextSibling || null);
        }

        // Reparent container back to node
        if (this._nodeParent) {
            this._nodeParent.insertBefore(this.container, this._nodeSibling);
        }
        this.container.style.flex = "";
        this.container.style.overflow = "";

        // Hide overlay
        this._fullscreenOverlay.style.display = "none";

        // Restore heights
        this.isFullscreen = false;
        EditorWidget._activeFullscreen = null;
        this._timelineHeight = TIMELINE_HEIGHT;
        this._galleryHeight = GALLERY_HEIGHT;
        this._fullscreenBtn.textContent = "⛶";
        this._fullscreenBtn.title = "Toggle fullscreen";

        this._renderTimeline();

        // Restore node size to what it was before fullscreen
        if (this._savedNodeSize) {
            this.node.setSize?.(this._savedNodeSize);
            this._savedNodeSize = null;
        } else {
            this.node.setSize?.(this.node.computeSize?.());
        }
    }

    _toggleFullscreen() {
        if (this.isFullscreen) {
            this._exitFullscreen();
        } else {
            this._enterFullscreen();
        }
    }

    // ── Keyboard Events ──────────────────────────────────────────────
    _setupKeyboardEvents() {
        this._keyHandler = (e) => {
            // Guard: don't fire when typing in inputs (except Ctrl+Z/Y for undo/redo)
            const tag = document.activeElement?.tagName;
            const isUndo = (e.ctrlKey || e.metaKey) && (e.key === "z" || e.key === "Z" || e.key === "y" || e.key === "Y");
            if ((tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") && !isUndo) return;

            // Guard: only handle keys when our editor is focused
            // (fullscreen always focused, node mode only when user clicked inside)
            if (!this.isFullscreen && !this._editorFocused) return;

            const ctrl = e.ctrlKey || e.metaKey;
            const shift = e.shiftKey;
            const key = e.key;

            // Helper: stop event from reaching ComfyUI's keyboard handlers
            const consume = () => { e.preventDefault(); e.stopPropagation(); e.stopImmediatePropagation(); };

            // ── Escape ──
            if (key === "Escape") {
                if (this.isFullscreen) { consume(); this._exitFullscreen(); }
                return;
            }

            // ── Undo / Redo ──
            if (ctrl && key === "z" && !shift) { consume(); this._undo(); return; }
            if (ctrl && (key === "y" || (key === "z" && shift))) { consume(); this._redo(); return; }

            // ── Delete ──
            if (key === "Delete" || key === "Backspace") {
                consume(); // Always consume Delete/Backspace to prevent ComfyUI node deletion
                if (this.selectedItems.length > 0) {
                    // Filter out locked-lane items before delete
                    this.selectedItems = this.selectedItems.filter(s => {
                        if (s.type === "clip") return !this._isLaneLocked(TRACK_TYPE.VIDEO, s.data.track_index || 0);
                        if (s.type === "audio") return !this._isLaneLocked(TRACK_TYPE.AUDIO, s.data.lane_index || 0);
                        return true;
                    });
                    if (this.selectedItems.length > 0) this._deleteSelectedItems();
                }
                return;
            }

            // ── Space = play/pause ──
            if (key === " ") {
                consume();
                if (this.isFullscreen) this._togglePlayback();
                return;
            }

            // ── Arrow keys: frame navigation ──
            if (key === "ArrowLeft") {
                consume();
                const step = shift ? 10 : 1;
                this.playhead = Math.max(0, this.playhead - step);
                this._onPlayheadChange();
                return;
            }
            if (key === "ArrowRight") {
                consume();
                const step = shift ? 10 : 1;
                this.playhead = Math.min(this.totalFrames, this.playhead + step);
                this._onPlayheadChange();
                return;
            }

            // ── Home / End ──
            if (key === "Home") {
                consume();
                this.playhead = 0;
                this._onPlayheadChange();
                return;
            }
            if (key === "End") {
                consume();
                this.playhead = this.totalFrames;
                this._onPlayheadChange();
                return;
            }

            // ── I / O = set in/out points (selection) ──
            if (key === "i" || key === "I") {
                consume();
                this.selectionStart = this.playhead;
                if (this.selectionEnd < this.selectionStart) this.selectionEnd = this.selectionStart;
                this._setWidgetValue("selection_start", this.selectionStart);
                this._renderTimeline();
                this._updateInfoLabel();
                this._updateToolbar();
                return;
            }
            if (key === "o" || key === "O") {
                consume();
                this.selectionEnd = this.playhead;
                if (this.selectionStart > this.selectionEnd) this.selectionStart = this.selectionEnd;
                this._setWidgetValue("selection_end", this.selectionEnd);
                this._renderTimeline();
                this._updateInfoLabel();
                this._updateToolbar();
                return;
            }

            // ── X = clear selection (in/out points) ──
            if (key === "x" || key === "X") {
                consume();
                this.selectionStart = 0;
                this.selectionEnd = 0;
                this._setWidgetValue("selection_start", 0);
                this._setWidgetValue("selection_end", 0);
                this._renderTimeline();
                this._updateInfoLabel();
                this._updateToolbar();
                return;
            }

            // ── C = toggle razor mode ──
            if (key === "c" || key === "C") {
                consume();
                this._razorMode = !this._razorMode;
                this._updateToolbar();
                return;
            }

            // ── S = toggle snapping ──
            if (key === "s" || key === "S") {
                consume();
                this.snappingEnabled = !this.snappingEnabled;
                this._updateToolbar();
                return;
            }

            // ── T = toggle timecode ──
            if (key === "t" || key === "T") { consume(); this._toggleTimecodeMode(); this._updateToolbar(); return; }

            // ── F = fit to view, Shift+F = zoom to selection ──
            if (key === "f" || key === "F") {
                consume();
                if (e.shiftKey && this.selectionStart < this.selectionEnd) {
                    const canvas = this.timelineCanvas;
                    const rect = canvas.parentElement?.getBoundingClientRect();
                    const width = rect ? Math.floor(rect.width) : 400;
                    const margin = width * 0.03;
                    const availableWidth = width - this._labelW - margin;
                    const range = this.selectionEnd - this.selectionStart;
                    if (range > 0 && availableWidth > 0) {
                        this.pixelsPerFrame = Math.max(0.2, Math.min(40, availableWidth / range));
                        this.scrollX = this.selectionStart;
                    }
                    this._renderTimeline();
                } else {
                    this._fitToView();
                }
                return;
            }

            // ── ? = shortcut overlay ──
            if (key === "?") { consume(); this._showShortcutOverlay(); return; }

            // ── Zoom: +/- ──
            if (key === "=" || key === "+") { consume(); this._zoom(1); return; }
            if (key === "-" || key === "_") { consume(); this._zoom(-1); return; }
        };
        // Use capture phase to intercept BEFORE ComfyUI's handlers
        document.addEventListener("keydown", this._keyHandler, true);

        // Track editor focus: set when clicking inside editor, clear when clicking outside
        this._focusHandler = (e) => {
            if (this.isFullscreen) {
                // In fullscreen, editor always has focus
                this._editorFocused = true;
            } else {
                // In node mode, check if click is inside our container
                this._editorFocused = !!(this.container?.contains(e.target));
            }
        };
        document.addEventListener("mousedown", this._focusHandler, true);
    }

    /** Called whenever the playhead changes position (arrow keys, etc.). */
    _onPlayheadChange() {
        this._renderTimeline();
        if (this.isFullscreen) this._renderViewportFrame();
        this._updateInfoLabel();
    }

    // ── Zoom ───────────────────────────────────────────────────────────
    _zoom(dir) {
        const oldPPF = this.pixelsPerFrame;
        this.pixelsPerFrame = Math.max(0.2, Math.min(40, this.pixelsPerFrame + dir * 0.5));
        if (this.pixelsPerFrame !== oldPPF) {
            this._renderTimeline();
        }
    }

    // ── UI Scale ──────────────────────────────────────────────────────
    _setUiScale(value) {
        this._uiScale = Math.round(Math.max(0.7, Math.min(2.0, value)) * 10) / 10;
        localStorage.setItem("ltx-editor-ui-scale", this._uiScale.toString());
        if (this._scaleLabel) this._scaleLabel.textContent = `${Math.round(this._uiScale * 100)}%`;
        this._applyUiScale();
    }

    _applyUiScale() {
        const s = this._uiScale;
        // Scale HTML bars via CSS transform
        for (const el of [this._sceneBar, this._toolbar, this._infoBar]) {
            if (!el) continue;
            el.style.transform = s !== 1.0 ? `scale(${s})` : "";
            el.style.transformOrigin = "top left";
            el.style.width = s !== 1.0 ? `${100 / s}%` : "";
        }
        this._renderTimeline();
        // Notify ComfyUI that our size changed
        if (this.node) this.node.setDirtyCanvas?.(true, true);
    }

    _canvasMouseCoords(e) {
        const rect = this.timelineCanvas.getBoundingClientRect();
        const s = this._uiScale;
        return {
            x: (e.clientX - rect.left) / s,
            y: (e.clientY - rect.top) / s
        };
    }

    // ── Undo / Redo ───────────────────────────────────────────────────

    // ── Thumbnail Strip & Waveform Caches ───────────────────────────────

    _getOrLoadThumbStrip(assetId) {
        const entry = this._thumbStripCache[assetId];
        if (entry) return entry.loaded ? entry : null;

        // Start loading
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const cache = { img: new Image(), frameWidth: 0, numFrames: 0, loaded: false };
        this._thumbStripCache[assetId] = cache;

        // Fetch info first
        fetch(api.apiURL(`/ltx-editor/project/${dirName}/thumbnail_strip/${assetId}?info=1`))
            .then(r => r.ok ? r.json() : null)
            .then(info => {
                if (!info) return;
                cache.frameWidth = info.frame_width;
                cache.numFrames = info.num_frames;
                cache.img.onload = () => {
                    cache.loaded = true;
                    this._renderTimeline();
                };
                cache.img.src = api.apiURL(`/ltx-editor/project/${dirName}/thumbnail_strip/${assetId}`);
            })
            .catch(() => {});

        return null;
    }

    _getOrLoadWaveform(assetId) {
        const entry = this._waveformCache[assetId];
        if (entry) return entry.loaded ? entry : null;

        const dirName = this.projectDir.split(/[/\\]/).pop();
        const cache = { peaks: [], numBuckets: 0, loaded: false };
        this._waveformCache[assetId] = cache;

        fetch(api.apiURL(`/ltx-editor/project/${dirName}/waveform/${assetId}`))
            .then(r => r.ok ? r.json() : null)
            .then(data => {
                if (!data) return;
                cache.peaks = data.peaks;
                cache.numBuckets = data.num_buckets;
                cache.loaded = true;
                this._renderTimeline();
            })
            .catch(() => {});

        return null;
    }

    // ── Undo / Redo ──────────────────────────────────────────────────────

    /** Capture a snapshot of the active scene BEFORE a mutation. */
    _pushUndo(label = "edit") {
        if (!this.activeScene || !this.activeSceneId) return;
        // Deep-clone the scene dict as snapshot
        const snapshot = JSON.parse(JSON.stringify(this.activeScene));
        this._undoStack.push({
            sceneId: this.activeSceneId,
            snapshot,
            label,
        });
        // Trim to max size
        if (this._undoStack.length > this._maxUndoSteps) {
            this._undoStack.shift();
        }
        // Any new action clears the redo stack
        this._redoStack = [];
    }

    async _undo() {
        if (this._undoStack.length === 0) return;
        const entry = this._undoStack.pop();

        // Save current state to redo stack before restoring
        if (this.activeScene && this.activeSceneId === entry.sceneId) {
            this._redoStack.push({
                sceneId: this.activeSceneId,
                snapshot: JSON.parse(JSON.stringify(this.activeScene)),
                label: entry.label,
            });
        }

        await this._restoreScene(entry.sceneId, entry.snapshot);
    }

    async _redo() {
        if (this._redoStack.length === 0) return;
        const entry = this._redoStack.pop();

        // Save current state to undo stack before restoring
        if (this.activeScene && this.activeSceneId === entry.sceneId) {
            this._undoStack.push({
                sceneId: this.activeSceneId,
                snapshot: JSON.parse(JSON.stringify(this.activeScene)),
                label: entry.label,
            });
        }

        await this._restoreScene(entry.sceneId, entry.snapshot);
    }

    async _restoreScene(sceneId, snapshot) {
        if (!this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();

        try {
            const resp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}/scenes/${sceneId}/restore`), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(snapshot),
            });
            if (resp.ok) {
                // If we're restoring a different scene, switch to it
                if (this.activeSceneId !== sceneId) {
                    this.activeSceneId = sceneId;
                }
                await this._fetchScenes();
                this._renderTimeline();
                this._renderViewportFrame();
            }
        } catch (e) {
            console.warn("[LTX Editor] Undo/redo restore failed:", e);
        }
    }

    // ── Widget Value Helpers ───────────────────────────────────────────
    _setWidgetValue(name, value) {
        const widget = this.node.widgets?.find(w => w.name === name);
        if (widget) {
            widget.value = value;
        }
    }

    // ── Public API ─────────────────────────────────────────────────────
    updateProject(projectDir) {
        if (projectDir === this.projectDir) return;
        this.projectDir = projectDir;
        this.activeSceneId = "";
        this.activeScene = null;
        this.scenes = [];
        this.sceneLabel.textContent = "Loading...";

        // Stop playback and clear video cache on project change
        this._stopPlayback();
        this._clearVideoCache();

        // Fetch project settings (fps, resolution)
        this._fetchProjectSettings();

        // Fetch assets first (triggers audio duration repair), then scenes
        this._fetchAssets().then(() => this._fetchScenes());
        this._renderTimeline();
    }

    async _fetchProjectSettings() {
        if (!this.projectDir) return;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        try {
            const resp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}`));
            if (resp.ok) {
                const data = await resp.json();
                this.fps = data.fps || 24;
                if (data.resolution) {
                    this.sceneWidth = data.resolution[0] || 768;
                    this.sceneHeight = data.resolution[1] || 512;
                }
                // Update viewport header if it exists
                if (this._vpHeaderText) {
                    this._vpHeaderText.textContent = `${this.sceneWidth}×${this.sceneHeight} @ ${this.fps}fps`;
                }
            }
        } catch (e) {
            console.warn("[LTX Editor] Failed to fetch project settings:", e);
        }
    }

    _clearVideoCache() {
        for (const key of Object.keys(this._videoCache)) {
            const v = this._videoCache[key];
            if (v.pause) v.pause();
            if (v._blobUrl) URL.revokeObjectURL(v._blobUrl);
            if (v.removeAttribute) v.removeAttribute("src");
            if (v.load) v.load();
        }
        this._videoCache = {};
        for (const key of Object.keys(this._audioCacheMap)) {
            const a = this._audioCacheMap[key];
            a.pause();
            if (a._blobUrl) URL.revokeObjectURL(a._blobUrl);
            a.removeAttribute("src");
        }
        this._audioCacheMap = {};
    }

    // ── Viewport Rendering ──────────────────────────────────────────

    _resizeViewportCanvas() {
        if (!this._vpCanvas || !this._fsViewportContent) return;
        const rect = this._fsViewportContent.getBoundingClientRect();
        const containerW = rect.width;
        const containerH = rect.height;
        if (containerW <= 0 || containerH <= 0) return;

        const aspect = this.sceneWidth / this.sceneHeight;
        let canvasW, canvasH;
        if (containerW / containerH > aspect) {
            // Container is wider than scene aspect — fit to height
            canvasH = Math.floor(containerH);
            canvasW = Math.floor(canvasH * aspect);
        } else {
            // Container is taller — fit to width
            canvasW = Math.floor(containerW);
            canvasH = Math.floor(canvasW / aspect);
        }

        this._vpCanvas.width = canvasW;
        this._vpCanvas.height = canvasH;
        this._vpCanvas.style.width = canvasW + "px";
        this._vpCanvas.style.height = canvasH + "px";

        this._renderViewportFrame();
    }

    _getClipAtFrame(frame) {
        if (!this.activeScene?.clips) return null;
        let best = null;
        for (const clip of this.activeScene.clips) {
            if (frame >= clip.timeline_start_frame && frame < clip.timeline_end_frame) {
                if (this._isLaneHidden(TRACK_TYPE.VIDEO, clip.track_index || 0)) continue;
                if (!best || (clip.track_index || 0) > (best.track_index || 0)) {
                    best = clip;
                }
            }
        }
        return best;
    }

    /** Get all non-hidden clips at a given frame, sorted bottom-up (lowest track_index first) */
    _getClipsAtFrame(frame) {
        if (!this.activeScene?.clips) return [];
        return this.activeScene.clips
            .filter(clip => frame >= clip.timeline_start_frame && frame < clip.timeline_end_frame)
            .filter(clip => !this._isLaneHidden(TRACK_TYPE.VIDEO, clip.track_index || 0))
            .sort((a, b) => (a.track_index || 0) - (b.track_index || 0));
    }

    _getAudioAtFrame(frame) {
        if (!this.activeScene?.audio_tracks) return null;
        for (const track of this.activeScene.audio_tracks) {
            if (frame >= track.timeline_start_frame && frame < track.timeline_end_frame) {
                return track;
            }
        }
        return null;
    }

    _getAudioTracksAtFrame(frame) {
        if (!this.activeScene?.audio_tracks) return [];
        return this.activeScene.audio_tracks.filter(
            a => frame >= a.timeline_start_frame && frame < a.timeline_end_frame
                && !this._isLaneHidden(TRACK_TYPE.AUDIO, a.lane_index || 0)
        );
    }

    _buildViewURL(sourcePath) {
        if (!this.projectDir || !sourcePath) return null;
        const dirName = this.projectDir.split(/[/\\]/).pop();
        const fileName = sourcePath.split(/[/\\]/).pop();
        const subPath = sourcePath.split(/[/\\]/).slice(0, -1).join("/");
        const subfolder = `ltx_projects/${dirName}/${subPath}`;
        return api.apiURL(`/view?filename=${encodeURIComponent(fileName)}&subfolder=${encodeURIComponent(subfolder)}&type=output`);
    }

    _getOrCreateVideo(sourcePath) {
        if (this._videoCache[sourcePath]) return this._videoCache[sourcePath];

        // LRU eviction: max 8 cached videos (increased for multi-layer compositing)
        const keys = Object.keys(this._videoCache).filter(k => !k.startsWith("guide_"));
        if (keys.length >= 8) {
            const oldest = keys[0];
            const old = this._videoCache[oldest];
            old.pause();
            // Revoke blob URL to free memory
            if (old._blobUrl) URL.revokeObjectURL(old._blobUrl);
            old.removeAttribute("src");
            old.load();
            delete this._videoCache[oldest];
        }

        const url = this._buildViewURL(sourcePath);
        if (!url) return null;

        // Create video element — load as blob for proper seeking support
        // (ComfyUI's /view endpoint doesn't support HTTP Range requests,
        //  so browser can't seek streaming video. Blob URLs fix this.)
        const video = document.createElement("video");
        video.preload = "auto";
        video.muted = true;
        video.playsInline = true;

        fetch(url)
            .then(resp => resp.blob())
            .then(blob => {
                const blobUrl = URL.createObjectURL(blob);
                video._blobUrl = blobUrl;
                video.src = blobUrl;
            })
            .catch(err => {
                console.warn("[LTX] Failed to load video as blob, falling back to direct URL:", err);
                video.crossOrigin = "anonymous";
                video.src = url;
            });

        this._videoCache[sourcePath] = video;
        return video;
    }

    _getOrCreateAudio(sourcePath) {
        if (this._audioCacheMap[sourcePath]) return this._audioCacheMap[sourcePath];
        const url = this._buildViewURL(sourcePath);
        if (!url) return null;

        // Load audio as blob for proper seeking support
        const audio = document.createElement("audio");
        audio.preload = "auto";

        fetch(url)
            .then(resp => resp.blob())
            .then(blob => {
                const blobUrl = URL.createObjectURL(blob);
                audio._blobUrl = blobUrl;
                audio.src = blobUrl;
            })
            .catch(err => {
                console.warn("[LTX] Failed to load audio as blob, falling back to direct URL:", err);
                audio.src = url;
            });

        this._audioCacheMap[sourcePath] = audio;
        return audio;
    }

    _renderViewportFrame() {
        if (!this._vpCanvas || !this._vpCtx) return;
        const ctx = this._vpCtx;
        const w = this._vpCanvas.width;
        const h = this._vpCanvas.height;
        if (w <= 0 || h <= 0) return;

        // Update transport UI
        this._updateTransportUI();

        // Guide images have absolute priority over video
        const guide = this._getGuideAtFrame(this.playhead);
        if (guide) {
            // Cancel any pending video seek
            if (this._seekAbort) {
                this._seekAbort();
                this._seekAbort = null;
            }
            this._drawGuideToViewport(guide);
            return;
        }

        const clips = this._getClipsAtFrame(this.playhead);

        if (clips.length === 0) {
            // No clip and no guide — show frame number centered
            ctx.fillStyle = "#000";
            ctx.fillRect(0, 0, w, h);
            ctx.fillStyle = "rgba(255,255,255,0.15)";
            ctx.font = `${Math.max(16, h / 12)}px monospace`;
            ctx.textAlign = "center";
            ctx.textBaseline = "middle";
            ctx.fillText(`Frame ${this.playhead}`, w / 2, h / 2);
            return;
        }

        // Single clip: use existing fast path
        if (clips.length === 1) {
            const clip = clips[0];
            const sourceFrame = this.playhead - clip.timeline_start_frame + (clip.source_in_frame || 0);
            const sourceTime = sourceFrame / this.fps;
            this._viewportClipOpacity = clip.opacity ?? 1.0;

            const video = this._getOrCreateVideo(clip.source_path);
            if (!video) return;

            if (video.readyState >= 2) {
                if (this.isPlaying) {
                    this._drawVideoToCanvas(video);
                } else {
                    this._seekVideoAndDraw(video, sourceTime);
                }
            } else {
                ctx.fillStyle = "#000";
                ctx.fillRect(0, 0, w, h);
                ctx.fillStyle = "rgba(255,255,255,0.3)";
                ctx.font = "14px sans-serif";
                ctx.textAlign = "center";
                ctx.textBaseline = "middle";
                ctx.fillText("Loading video...", w / 2, h / 2);

                const onReady = () => {
                    video.removeEventListener("canplay", onReady);
                    video.removeEventListener("loadeddata", onReady);
                    this._renderViewportFrame();
                };
                video.addEventListener("canplay", onReady);
                video.addEventListener("loadeddata", onReady);
            }
            return;
        }

        // Multi-layer compositing: multiple clips at this frame
        if (this.isPlaying) {
            // During playback — draw all layers using current video frame positions
            ctx.fillStyle = "#000";
            ctx.fillRect(0, 0, w, h);
            for (const clip of clips) {
                const video = this._getOrCreateVideo(clip.source_path);
                if (!video || video.readyState < 2) continue;
                const opacity = clip.opacity ?? 1.0;
                if (opacity <= 0) continue;
                this._drawVideoToCanvasRaw(video, ctx, w, h, opacity);
            }
        } else {
            // Scrubbing — seek all videos then composite
            this._renderViewportComposite(this.playhead, clips);
        }
    }

    /** Multi-layer composite rendering (scrub mode) */
    async _renderViewportComposite(frame, clips) {
        const ctx = this._vpCtx;
        const w = this._vpCanvas.width;
        const h = this._vpCanvas.height;

        // Cancel any previous seek
        if (this._seekAbort) {
            this._seekAbort();
            this._seekAbort = null;
        }

        let cancelled = false;
        this._seekAbort = () => { cancelled = true; };

        // Pre-seek all videos in parallel
        const seekPromises = clips.map(clip => {
            const video = this._getOrCreateVideo(clip.source_path);
            if (!video || video.readyState < 2) return Promise.resolve(null);
            const sourceFrame = frame - clip.timeline_start_frame + (clip.source_in_frame || 0);
            const targetTime = sourceFrame / this.fps;
            if (Math.abs(video.currentTime - targetTime) < 0.02) return Promise.resolve(video);
            return new Promise(resolve => {
                const handler = () => { video.removeEventListener("seeked", handler); resolve(video); };
                video.addEventListener("seeked", handler);
                video.currentTime = targetTime;
            });
        });

        const videos = await Promise.all(seekPromises);
        if (cancelled) return;
        this._seekAbort = null;

        // Clear + draw all layers
        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, w, h);

        for (let i = 0; i < clips.length; i++) {
            const video = videos[i];
            if (!video) continue;
            const opacity = clips[i].opacity ?? 1.0;
            if (opacity <= 0) continue;
            this._drawVideoToCanvasRaw(video, ctx, w, h, opacity);
        }
    }

    _seekVideoAndDraw(video, targetTime) {
        // Cancel any previous pending seek
        if (this._seekAbort) {
            this._seekAbort();
            this._seekAbort = null;
        }

        // Skip if already at the right time
        if (Math.abs(video.currentTime - targetTime) < 0.005) {
            this._drawVideoToCanvas(video);
            return;
        }

        // Pause before seeking
        video.pause();

        let cancelled = false;
        this._seekAbort = () => { cancelled = true; };

        const prevTime = video.currentTime;

        // Set up multiple ways to detect seek completion
        const drawOnce = (method) => {
            if (cancelled) return;
            this._seekAbort = null;
            console.log(`[LTX Scrub] Drew frame via ${method}: target=${targetTime.toFixed(3)}, actual=${video.currentTime.toFixed(3)}, prev=${prevTime.toFixed(3)}, readyState=${video.readyState}, seekable=${video.seekable.length > 0 ? video.seekable.start(0).toFixed(1) + '-' + video.seekable.end(0).toFixed(1) : 'none'}`);
            this._drawVideoToCanvas(video);
        };

        // Method 1: seeked event
        const onSeeked = () => {
            video.removeEventListener("seeked", onSeeked);
            clearTimeout(fallbackTimer);
            drawOnce("seeked");
        };
        video.addEventListener("seeked", onSeeked);

        // Method 2: fallback polling timer (in case seeked event doesn't fire)
        const fallbackTimer = setTimeout(() => {
            video.removeEventListener("seeked", onSeeked);
            console.warn(`[LTX Scrub] seeked event did not fire in 150ms, using fallback. target=${targetTime.toFixed(3)}, actual=${video.currentTime.toFixed(3)}`);
            drawOnce("fallback");
        }, 150);

        // Initiate seek
        video.currentTime = targetTime;
    }

    _getGuideAtFrame(frame) {
        if (!this.activeScene?.guide_frames) return null;
        // Find the closest guide at or before this frame
        let closest = null;
        for (const g of this.activeScene.guide_frames) {
            const idx = g.frame_index === -1 ? this.totalFrames - 1 : g.frame_index;
            if (idx === frame) return g;
        }
        return null;
    }

    _drawGuideToViewport(guide) {
        if (!this._vpCtx || !this._vpCanvas) return;
        // Find the asset for this guide
        const asset = this.assets.image?.find(a => a.asset_id === guide.asset_id);
        if (!asset) return;

        const url = this._buildViewURL(asset.path);
        if (!url) return;

        // Use a cached image element
        const cacheKey = `guide_${guide.asset_id}`;
        if (!this._videoCache[cacheKey]) {
            const img = new Image();
            img.crossOrigin = "anonymous";
            img.onload = () => {
                this._videoCache[cacheKey] = img;
                this._drawImageToCanvas(img);
            };
            img.src = url;
            return;
        }
        this._drawImageToCanvas(this._videoCache[cacheKey]);
    }

    _drawImageToCanvas(img) {
        if (!this._vpCtx || !this._vpCanvas) return;
        const ctx = this._vpCtx;
        const cw = this._vpCanvas.width;
        const ch = this._vpCanvas.height;

        const iw = img.naturalWidth || img.width || cw;
        const ih = img.naturalHeight || img.height || ch;
        const iAspect = iw / ih;
        const cAspect = cw / ch;

        let dx, dy, dw, dh;
        if (iAspect > cAspect) {
            dw = cw; dh = cw / iAspect; dx = 0; dy = (ch - dh) / 2;
        } else {
            dh = ch; dw = ch * iAspect; dx = (cw - dw) / 2; dy = 0;
        }

        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, cw, ch);
        try { ctx.drawImage(img, dx, dy, dw, dh); } catch (e) {}
    }

    _drawVideoToCanvas(video) {
        if (!this._vpCtx || !this._vpCanvas) return;
        const ctx = this._vpCtx;
        const cw = this._vpCanvas.width;
        const ch = this._vpCanvas.height;

        // Draw video maintaining aspect ratio within canvas
        const vw = video.videoWidth || cw;
        const vh = video.videoHeight || ch;
        const vAspect = vw / vh;
        const cAspect = cw / ch;

        let dx, dy, dw, dh;
        if (vAspect > cAspect) {
            dw = cw;
            dh = cw / vAspect;
            dx = 0;
            dy = (ch - dh) / 2;
        } else {
            dh = ch;
            dw = ch * vAspect;
            dx = (cw - dw) / 2;
            dy = 0;
        }

        ctx.fillStyle = "#000";
        ctx.fillRect(0, 0, cw, ch);
        const opacity = this._viewportClipOpacity ?? 1.0;
        if (opacity < 1.0) ctx.globalAlpha = opacity;
        try {
            ctx.drawImage(video, dx, dy, dw, dh);
        } catch (e) {
            // Video may not be ready yet
        }
        if (opacity < 1.0) ctx.globalAlpha = 1.0;
    }

    /** Draw video to canvas WITHOUT clearing — for multi-layer compositing */
    _drawVideoToCanvasRaw(video, ctx, cw, ch, opacity) {
        const vw = video.videoWidth || cw;
        const vh = video.videoHeight || ch;
        const vAspect = vw / vh;
        const cAspect = cw / ch;

        let dx, dy, dw, dh;
        if (vAspect > cAspect) {
            dw = cw; dh = cw / vAspect; dx = 0; dy = (ch - dh) / 2;
        } else {
            dh = ch; dw = ch * vAspect; dx = (cw - dw) / 2; dy = 0;
        }

        if (opacity < 1.0) ctx.globalAlpha = opacity;
        try { ctx.drawImage(video, dx, dy, dw, dh); } catch (e) {}
        if (opacity < 1.0) ctx.globalAlpha = 1.0;
    }

    _updateTransportUI() {
        if (this._vpFrameCounter) {
            if (this._timecodeMode === "timecode") {
                this._vpFrameCounter.textContent = `${this._frameToTimecode(this.playhead)} / ${this._frameToTimecode(this.totalFrames)}`;
            } else {
                this._vpFrameCounter.textContent = `Frame ${this.playhead} / ${this.totalFrames}`;
            }
        }
        if (this._vpProgressFill && this.totalFrames > 0) {
            this._vpProgressFill.style.width = `${(this.playhead / this.totalFrames) * 100}%`;
        }
        if (this._vpFrameLabel) {
            this._vpFrameLabel.textContent = this._timecodeMode === "timecode" ? this._frameToTimecode(this.playhead) : `f${this.playhead}`;
        }
    }

    // ── Playback ──────────────────────────────────────────────────────

    _togglePlayback() {
        if (this.isPlaying) {
            this._stopPlayback();
        } else {
            this._startPlayback();
        }
    }

    _startPlayback() {
        if (this.isPlaying) return;
        this.isPlaying = true;
        if (this._vpPlayBtn) this._vpPlayBtn.textContent = "⏸";

        this._playbackStartTime = performance.now();
        this._playbackStartFrame = this.playhead;

        // Cancel any pending scrub seek
        if (this._seekAbort) {
            this._seekAbort();
            this._seekAbort = null;
        }

        // Start video elements for all visible clips at current frame
        const visibleClips = this._getClipsAtFrame(this.playhead);
        this._activePlaybackVideos = [];
        for (const clip of visibleClips) {
            const video = this._getOrCreateVideo(clip.source_path);
            if (!video) continue;
            const sourceFrame = this.playhead - clip.timeline_start_frame + (clip.source_in_frame || 0);
            const sourceTime = sourceFrame / this.fps;
            video.muted = true;

            const startVideoPlayback = (v) => {
                const onSeeked = () => {
                    v.removeEventListener("seeked", onSeeked);
                    v.play().catch(() => {});
                };
                if (Math.abs(v.currentTime - sourceTime) > 0.01) {
                    v.addEventListener("seeked", onSeeked);
                    v.currentTime = sourceTime;
                    setTimeout(() => {
                        v.removeEventListener("seeked", onSeeked);
                        v.play().catch(() => {});
                    }, 200);
                } else {
                    v.play().catch(() => {});
                }
            };

            if (video.readyState >= 2) {
                startVideoPlayback(video);
            } else {
                const onReady = () => {
                    video.removeEventListener("canplay", onReady);
                    video.removeEventListener("loadeddata", onReady);
                    startVideoPlayback(video);
                };
                video.addEventListener("canplay", onReady);
                video.addEventListener("loadeddata", onReady);
            }
            this._activePlaybackVideos.push(video);
        }
        // Backward compat
        this._activePlaybackVideo = this._activePlaybackVideos.length > 0 ? this._activePlaybackVideos[this._activePlaybackVideos.length - 1] : null;

        // Start audio tracks
        const audioTracks = this._getAudioTracksAtFrame(this.playhead);
        this._activePlaybackAudios = [];
        for (const track of audioTracks) {
            if (track.muted) continue;
            const audio = this._getOrCreateAudio(track.source_path);
            if (audio) {
                const audioFrame = this.playhead - track.timeline_start_frame + (track.source_in_frame || 0);
                audio.currentTime = audioFrame / this.fps;
                audio.volume = track.volume ?? 1.0;
                audio.play().catch(() => {});
                this._activePlaybackAudios.push(audio);
            }
        }

        this._playbackRAF = requestAnimationFrame((t) => this._playbackTick(t));
    }

    _playbackTick(timestamp) {
        if (!this.isPlaying) return;

        const elapsed = (timestamp - this._playbackStartTime) / 1000;
        const newFrame = this._playbackStartFrame + Math.floor(elapsed * this.fps);

        if (newFrame >= this.totalFrames) {
            this.playhead = this.totalFrames;
            this._stopPlayback();
            return;
        }

        const prevFrame = this.playhead;
        this.playhead = newFrame;

        // Detect clip boundary crossing (multi-layer aware)
        const prevClips = this._getClipsAtFrame(prevFrame);
        const currClips = this._getClipsAtFrame(newFrame);
        const prevPaths = new Set(prevClips.map(c => c.source_path));
        const currPaths = new Set(currClips.map(c => c.source_path));

        // Stop videos no longer visible
        if (this._activePlaybackVideos) {
            this._activePlaybackVideos = this._activePlaybackVideos.filter(v => {
                const srcPath = Object.keys(this._videoCache).find(k => this._videoCache[k] === v);
                if (srcPath && !currPaths.has(srcPath)) { v.pause(); return false; }
                return true;
            });
        }

        // Start newly visible clip videos
        for (const clip of currClips) {
            if (!prevPaths.has(clip.source_path)) {
                const video = this._getOrCreateVideo(clip.source_path);
                if (video && video.readyState >= 2) {
                    const sf = newFrame - clip.timeline_start_frame + (clip.source_in_frame || 0);
                    video.currentTime = sf / this.fps;
                    video.muted = true;
                    video.play().catch(() => {});
                    if (!this._activePlaybackVideos) this._activePlaybackVideos = [];
                    this._activePlaybackVideos.push(video);
                }
            }
        }
        this._activePlaybackVideo = this._activePlaybackVideos?.length > 0 ? this._activePlaybackVideos[this._activePlaybackVideos.length - 1] : null;

        // Draw current frame
        this._renderViewportFrame();
        this._renderTimeline();

        this._playbackRAF = requestAnimationFrame((t) => this._playbackTick(t));
    }

    _stopPlayback() {
        if (this._playbackRAF) {
            cancelAnimationFrame(this._playbackRAF);
            this._playbackRAF = null;
        }
        this.isPlaying = false;
        if (this._vpPlayBtn) this._vpPlayBtn.textContent = "▶";

        // Pause active videos (multi-layer)
        if (this._activePlaybackVideos) {
            for (const v of this._activePlaybackVideos) v.pause();
            this._activePlaybackVideos = [];
        }
        if (this._activePlaybackVideo) {
            this._activePlaybackVideo.pause();
            this._activePlaybackVideo = null;
        }

        // Pause active audios
        for (const audio of this._activePlaybackAudios) {
            audio.pause();
        }
        this._activePlaybackAudios = [];
    }

    getElement() {
        return this.container;
    }

    getHeight() {
        if (this.isFullscreen) return 60;
        const s = this._uiScale;
        const barsH = (SCENE_BAR_HEIGHT + 24 + 24) * s; // scene bar + toolbar + info bar
        const timelineH = this._timelineHeight * s;
        const editorsH = ((this._promptEditorEl ? 30 : 0) + (this._itemEditorEl ? 30 : 0)) * s;
        return barsH + timelineH + this._galleryHeight + editorsH;
    }

    // ── File Import (drag-and-drop files from OS onto node) ────────────
    async _importFile(file) {
        if (!this.projectDir) return;

        // Upload to ComfyUI first, then import to project via API
        const formData = new FormData();
        formData.append("image", file, file.name);
        formData.append("overwrite", "true");

        try {
            // Upload to ComfyUI's input directory
            const uploadResp = await fetch(api.apiURL("/upload/image"), {
                method: "POST",
                body: formData,
            });

            if (!uploadResp.ok) {
                console.warn("[LTX Editor] Upload failed:", await uploadResp.text());
                return;
            }

            const uploadData = await uploadResp.json();
            const uploadedName = uploadData.name;

            // Now get the full path and import to project
            // ComfyUI stores uploads in its input directory
            const dirName = this.projectDir.split(/[/\\]/).pop();
            const importResp = await fetch(api.apiURL(`/ltx-editor/project/${dirName}/assets/import`), {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    source_path: uploadedName,
                    // The import endpoint needs the full path — let the server resolve it
                }),
            });

            if (importResp.ok) {
                console.log("[LTX Editor] Imported:", file.name);
                await this._fetchAssets();
            } else {
                console.warn("[LTX Editor] Import failed:", await importResp.text());
            }
        } catch (e) {
            console.warn("[LTX Editor] File import error:", e);
        }
    }
}

// Module-level guard: only one editor can be fullscreen at a time
EditorWidget._activeFullscreen = null;
