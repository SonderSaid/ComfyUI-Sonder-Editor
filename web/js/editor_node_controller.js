const { api } = window.comfyAPI.api;

import { getEditorSettings, updateEditorSettings } from "./editor_settings.js";
import { EditorWidget, buildProjectAssetViewURL, importFileIntoProject, replaceAssetInProject } from "./editor_widget.js";
import { loadMediaAsBlob, mountSharedAssetGallery } from "./shared_asset_gallery.js";

function style(el, cssText) {
    el.style.cssText = cssText;
    return el;
}

const CHROME = {
    panelMuted: "#10161d",
    panel: "#151c24",
    panelRaised: "#1b2430",
    panelRaisedHover: "#25313f",
    border: "#34414d",
    borderSoft: "#28313b",
    borderStrong: "#587089",
    text: "#dbe3ea",
    textDim: "#90a0af",
    textMuted: "#748291",
    accent: "#4a82ad",
    accentSoft: "#263a4d",
    accentSoftHover: "#314961",
    accentBorder: "#6686a3",
    warningSoft: "#45361f",
    warningBorder: "#9a7a42",
    warningText: "#efd79f",
    dangerSoft: "#44292d",
    dangerBorder: "#8f5f66",
    dangerText: "#efc0c4",
};

const BUTTON_STYLES = {
    muted: {
        background: CHROME.panelRaised,
        border: CHROME.border,
        text: CHROME.textDim,
    },
    subtle: {
        background: CHROME.panel,
        border: CHROME.border,
        text: CHROME.textDim,
    },
    primary: {
        background: CHROME.accentSoft,
        border: CHROME.accentBorder,
        text: "#f7fbff",
    },
    active: {
        background: CHROME.accent,
        border: "#7ea8c9",
        text: "#ffffff",
    },
};

function buttonStyle(variant = "muted", { padding = "6px 10px", radius = "6px", fontSize = "11px", fontWeight = "600" } = {}) {
    const palette = BUTTON_STYLES[variant] || BUTTON_STYLES.muted;
    return `
        background: ${palette.background};
        color: ${palette.text};
        border: 1px solid ${palette.border};
        border-radius: ${radius};
        padding: ${padding};
        cursor: pointer;
        font-size: ${fontSize};
        font-weight: ${fontWeight};
        box-shadow: inset 0 1px 0 rgba(255,255,255,0.04);
    `;
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

function formatQueueStatusLabel(status) {
    const raw = String(status || "pending").trim().toLowerCase();
    if (!raw) return "Pending";
    return raw.charAt(0).toUpperCase() + raw.slice(1);
}

function formatQueueTime(frame, fps, mode = "frames") {
    const safeFrame = Math.max(0, parseInt(frame, 10) || 0);
    if (mode !== "timecode") return String(safeFrame);
    const safeFps = Math.max(1, Number(fps) || 24);
    const totalSeconds = safeFrame / safeFps;
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = Math.floor(totalSeconds % 60);
    const f = Math.floor(safeFrame % safeFps);
    if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}:${String(f).padStart(2, "0")}`;
    return `${m}:${String(s).padStart(2, "0")}:${String(f).padStart(2, "0")}`;
}

function formatQueueSelectionSummary(job, options = {}) {
    const start = Math.max(0, parseInt(job?.selection_start, 10) || 0);
    const end = Math.max(start, parseInt(job?.selection_end, 10) || 0);
    const duration = end - start;
    const preContext = Math.max(0, parseInt(job?.pre_context_frames, 10) || 0);
    const postContext = Math.max(0, parseInt(job?.post_context_frames, 10) || 0);
    const fps = Math.max(1, Number(job?.scene_fps) || Number(options.fps) || 24);
    const mode = options.mode === "timecode" ? "timecode" : "frames";
    return `In: ${formatQueueTime(start, fps, mode)} Out: ${formatQueueTime(end, fps, mode)} (${formatQueueTime(duration, fps, mode)}) | Ctx: -${preContext}/+${postContext}`;
}

function groupQueueJobs(queue) {
    const groups = [];
    let index = 0;
    while (index < queue.length) {
        const job = queue[index];
        const batchId = String(job?.batch_id || "");
        if (!batchId) {
            groups.push({ type: "single", job });
            index += 1;
            continue;
        }

        const jobs = [job];
        index += 1;
        while (index < queue.length && String(queue[index]?.batch_id || "") === batchId) {
            jobs.push(queue[index]);
            index += 1;
        }

        if (jobs.length === 1) {
            groups.push({ type: "single", job });
            continue;
        }
        groups.push({ type: "batch", batchId, jobs });
    }
    return groups;
}

function readQueueBatchCollapseState(projectDir, settings = getEditorSettings()) {
    const projectKey = projectIdFromDir(projectDir);
    const collapsedByProject = settings?.layout?.queueBatchCollapsedByProject;
    const collapsedIds = projectKey && collapsedByProject && typeof collapsedByProject === "object"
        ? collapsedByProject[projectKey]
        : null;
    if (!Array.isArray(collapsedIds)) {
        return new Set();
    }
    return new Set(collapsedIds.filter((value) => typeof value === "string" && value));
}

function persistQueueBatchCollapseState(projectDir, collapsedIds) {
    const projectKey = projectIdFromDir(projectDir);
    if (!projectKey) return;
    updateEditorSettings({
        layout: {
            queueBatchCollapsedByProject: {
                [projectKey]: Array.from(collapsedIds)
                    .filter((value) => typeof value === "string" && value)
                    .sort(),
            },
        },
    });
}

function formatClockTime(seconds) {
    const safeSeconds = Math.max(0, Number(seconds) || 0);
    const totalSeconds = Math.floor(safeSeconds);
    const mins = Math.floor(totalSeconds / 60);
    const secs = totalSeconds % 60;
    return `${mins}:${String(secs).padStart(2, "0")}`;
}

function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
}

function renderDormantMediaScrubBar(mediaEl) {
    const wrap = style(document.createElement("div"), `
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px;
        border-radius: 8px;
        background: rgba(255,255,255,0.03);
        border: 1px solid ${CHROME.borderSoft};
    `);
    const track = style(document.createElement("div"), `
        position: relative;
        flex: 1 1 auto;
        height: 10px;
        border-radius: 999px;
        background: #1a2631;
        cursor: pointer;
        overflow: hidden;
    `);
    const fill = style(document.createElement("div"), `
        position: absolute;
        left: 0;
        top: 0;
        bottom: 0;
        width: 0;
        background: linear-gradient(90deg,#6fa7d8,#8fc0f0);
    `);
    const thumb = style(document.createElement("div"), `
        position: absolute;
        top: 50%;
        width: 12px;
        height: 12px;
        border-radius: 50%;
        background: #d9ebfb;
        border: 1px solid rgba(0,0,0,0.35);
        transform: translate(-50%,-50%);
        left: 100%;
        pointer-events: none;
        box-shadow: 0 1px 3px rgba(0,0,0,0.35);
    `);
    fill.appendChild(thumb);
    track.appendChild(fill);
    const label = style(document.createElement("div"), `
        color: #a9bccb;
        font-size: 10px;
        white-space: nowrap;
        min-width: 72px;
        text-align: right;
    `);
    wrap.append(track, label);

    let dragging = false;

    const duration = () => {
        const value = Number(mediaEl?.duration);
        return Number.isFinite(value) && value > 0 ? value : 0;
    };

    const updateUI = () => {
        const total = duration();
        const current = clamp(Number(mediaEl?.currentTime) || 0, 0, total || Number.MAX_SAFE_INTEGER);
        const ratio = total > 0 ? current / total : 0;
        fill.style.width = `${ratio * 100}%`;
        label.textContent = `${formatClockTime(current)} / ${formatClockTime(total)}`;
    };

    const seekFromClientX = (clientX) => {
        const total = duration();
        if (!total) return;
        const rect = track.getBoundingClientRect();
        const ratio = clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1);
        mediaEl.currentTime = ratio * total;
        updateUI();
    };

    const handlePointerMove = (event) => {
        if (!dragging) return;
        seekFromClientX(event.clientX);
    };

    const handlePointerUp = (event) => {
        if (!dragging) return;
        dragging = false;
        seekFromClientX(event.clientX);
        window.removeEventListener("mousemove", handlePointerMove);
        window.removeEventListener("mouseup", handlePointerUp);
    };

    track.addEventListener("mousedown", (event) => {
        event.preventDefault();
        dragging = true;
        seekFromClientX(event.clientX);
        window.addEventListener("mousemove", handlePointerMove);
        window.addEventListener("mouseup", handlePointerUp);
    });

    const mediaEvents = ["loadedmetadata", "durationchange", "timeupdate", "seeking", "play", "pause", "ended"];
    for (const eventName of mediaEvents) {
        mediaEl.addEventListener(eventName, updateUI);
    }
    updateUI();

    return {
        el: wrap,
        cleanup: () => {
            dragging = false;
            window.removeEventListener("mousemove", handlePointerMove);
            window.removeEventListener("mouseup", handlePointerUp);
            for (const eventName of mediaEvents) {
                mediaEl.removeEventListener(eventName, updateUI);
            }
        },
    };
}

function consumeDormantPointer(event, { preventDefault = false } = {}) {
    if (!event) return;
    if (preventDefault) {
        event.preventDefault();
    }
    event.stopPropagation();
    event.stopImmediatePropagation?.();
}

function clearDormantCanvas(canvas) {
    const ctx = canvas?.getContext?.("2d");
    if (!ctx || !canvas) return;
    ctx.fillStyle = "#000";
    ctx.fillRect(0, 0, Math.max(1, canvas.width || 1), Math.max(1, canvas.height || 1));
}

function drawDormantCanvasMessage(canvas, title, subtitle = "") {
    const ctx = canvas?.getContext?.("2d");
    if (!ctx || !canvas) return;
    const width = Math.max(1, canvas.width || 1);
    const height = Math.max(1, canvas.height || 1);
    clearDormantCanvas(canvas);
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "rgba(255,255,255,0.24)";
    ctx.font = `${Math.max(16, Math.floor(height / 12))}px monospace`;
    ctx.fillText(title, width / 2, height / 2 - (subtitle ? 12 : 0));
    if (subtitle) {
        ctx.fillStyle = "rgba(255,255,255,0.62)";
        ctx.font = `${Math.max(11, Math.floor(height / 24))}px sans-serif`;
        ctx.fillText(subtitle, width / 2, height / 2 + 16);
    }
}

function loadDormantPreviewImage(cache, src, onReady) {
    if (!src) return null;
    const cached = cache.get(src);
    if (cached?.img) return cached.img;
    if (cached?.loading) return null;
    const img = new Image();
    cache.set(src, { img: null, loading: true });
    img.onload = () => {
        cache.set(src, { img, loading: false });
        onReady?.();
    };
    img.onerror = () => {
        cache.set(src, { img: null, loading: false });
        onReady?.();
    };
    img.src = src;
    return null;
}

function drawDormantCanvasMedia(canvas, media, { opacity = 1 } = {}) {
    const ctx = canvas?.getContext?.("2d");
    if (!ctx || !canvas || !media) return false;
    const mediaWidth = Math.max(
        1,
        parseInt(media.videoWidth, 10)
            || parseInt(media.naturalWidth, 10)
            || parseInt(media.width, 10)
            || 1
    );
    const mediaHeight = Math.max(
        1,
        parseInt(media.videoHeight, 10)
            || parseInt(media.naturalHeight, 10)
            || parseInt(media.height, 10)
            || 1
    );
    const canvasWidth = Math.max(1, canvas.width || 1);
    const canvasHeight = Math.max(1, canvas.height || 1);
    const scale = Math.min(canvasWidth / mediaWidth, canvasHeight / mediaHeight);
    const drawWidth = Math.max(1, Math.round(mediaWidth * scale));
    const drawHeight = Math.max(1, Math.round(mediaHeight * scale));
    const drawX = Math.floor((canvasWidth - drawWidth) / 2);
    const drawY = Math.floor((canvasHeight - drawHeight) / 2);
    const prevAlpha = ctx.globalAlpha;
    if (opacity < 1) ctx.globalAlpha = Math.max(0, opacity);
    try {
        ctx.drawImage(media, drawX, drawY, drawWidth, drawHeight);
    } catch (error) {
        ctx.globalAlpha = prevAlpha;
        return false;
    }
    ctx.globalAlpha = prevAlpha;
    return true;
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
    return api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/assets/dormant?include_trashed=true`);
}

function buildQueueUrl(projectDir) {
    return api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/queue`);
}

function buildQueueJobUrl(projectDir, jobId) {
    return api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/queue/${jobId}`);
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

function isAudioLaneHidden(scene, laneIndex) {
    return !!scene?.audio_lane_configs?.[laneIndex || 0]?.hidden;
}

function pickPreviewTargetForFrame(projectDir, scene, assets, frame, fallbackDimensions = {}) {
    const fallbackFrame = Math.max(0, parseInt(frame, 10) || 0);
    const frameWidth = Math.max(0, parseInt(scene?.width, 10) || parseInt(fallbackDimensions.width, 10) || 0);
    const frameHeight = Math.max(0, parseInt(scene?.height, 10) || parseInt(fallbackDimensions.height, 10) || 0);
    const assetsByPath = new Map((assets || []).map(asset => [asset.path, asset]));
    const assetsById = new Map((assets || []).map(asset => [asset.asset_id, asset]));
    const isMissingAsset = (asset) => !asset || !!asset.missing;

    const activeClips = (scene?.clips || [])
        .filter(clip => fallbackFrame >= clip.timeline_start_frame && fallbackFrame < clip.timeline_end_frame)
        .filter(clip => !isVideoLaneHidden(scene, clip.track_index || 0))
        .sort((a, b) => (b.track_index || 0) - (a.track_index || 0));

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
    const guideAsset = guide ? assetsById.get(guide.asset_id) : null;
    const guidePreview = guideAsset && !isMissingAsset(guideAsset)
        ? {
            posterUrl: buildProjectAssetViewURL(projectDir, guideAsset.path),
        }
        : null;

    if (activeClips.length > 1) {
        const layers = activeClips
            .slice()
            .reverse()
            .map((clip) => {
                const asset = assetsByPath.get(clip.source_path);
                return {
                    key: clip.clip_id || `${clip.source_path}:${clip.timeline_start_frame || 0}:${clip.track_index || 0}`,
                    clip,
                    sourcePath: clip.source_path,
                    mediaUrl: !isMissingAsset(asset) ? buildProjectAssetViewURL(projectDir, clip.source_path) : "",
                    posterUrl: !isMissingAsset(asset) && asset?.has_thumbnail
                        ? api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/thumbnail/${asset.asset_id}`)
                        : null,
                    missing: isMissingAsset(asset),
                    opacity: Math.max(0, Math.min(1, Number(clip.opacity ?? 1))),
                };
            })
            .filter(layer => !layer.missing || layer.posterUrl);
        if (layers.length || guidePreview) {
            return {
                kind: "composite",
                label: `Frame ${fallbackFrame}`,
                subtitle: guidePreview ? `${activeClips.length} layers + guide` : `${activeClips.length} layers`,
                layers,
                guide: guidePreview,
                frameWidth,
                frameHeight,
            };
        }
        return {
            kind: "empty",
            label: `Frame ${fallbackFrame}`,
            subtitle: "Composite preview unavailable for the current layers.",
        };
    }

    if (activeClips.length === 1) {
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
            key: clip.clip_id || `${clip.source_path}:${clip.timeline_start_frame || 0}:${clip.track_index || 0}`,
            posterUrl: asset?.has_thumbnail
                ? api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/thumbnail/${asset.asset_id}`)
                : null,
            mediaUrl: buildProjectAssetViewURL(projectDir, clip.source_path),
            clip,
        };
    }

    if (guide) {
        if (isMissingAsset(guideAsset)) {
            return {
                kind: "missing",
                label: `Missing Guide ${guideFrame}`,
                subtitle: guideAsset?.name || guideAsset?.path?.split(/[/\\]/).pop() || "Guide asset entry not found.",
            };
        }
        if (guideAsset) {
            return {
                kind: "image",
                label: `Guide ${guideFrame}`,
                subtitle: guideAsset.name || guideAsset.path.split(/[/\\]/).pop() || "Guide",
                posterUrl: buildProjectAssetViewURL(projectDir, guideAsset.path),
            };
        }
    }

    return {
        kind: "empty",
        label: "No preview",
        subtitle: "No clip or guide at the current selection.",
    };
}

function pickPreviewTarget(projectDir, summary, scene, assets) {
    return pickPreviewTargetForFrame(
        projectDir,
        scene,
        assets,
        summary?.active_scene?.selection?.generation_start_frame || 0,
        {
            width: summary?.active_scene?.effective_width || summary?.active_scene?.width || 0,
            height: summary?.active_scene?.effective_height || summary?.active_scene?.height || 0,
        }
    );
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
            border: 1px solid ${CHROME.border};
            border-radius: 10px;
            background: linear-gradient(180deg, ${CHROME.panelRaised} 0%, ${CHROME.panelMuted} 100%);
            color: ${CHROME.text};
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
            color: #f0f4f8;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        `);
        projectTitle.textContent = summary?.name || state.projectName || "LTX Editor";
        const sceneTitle = style(document.createElement("div"), `
            color: ${CHROME.textDim};
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
        if (state.isFullscreenOpen) badges.push({ text: "Editor Active", color: CHROME.accentSoft, border: CHROME.accentBorder });
        if ((queueCounts.running || 0) > 0) badges.push({ text: `${queueCounts.running} Running`, color: "#264863", border: "#5d8db5" });
        if ((queueCounts.pending || 0) > 0) badges.push({ text: `${queueCounts.pending} Pending`, color: CHROME.warningSoft, border: CHROME.warningBorder });
        if (!badges.length && summary) badges.push({ text: "Idle", color: "#223128", border: "#4d6a58" });
        for (const badge of badges) {
            const pill = style(document.createElement("span"), `
                padding: 2px 8px;
                border-radius: 999px;
                background: ${badge.color};
                border: 1px solid ${badge.border};
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
                border: 1px solid ${CHROME.borderSoft};
                min-width: 0;
            `);
            const keyEl = style(document.createElement("div"), `
                color: ${CHROME.textDim};
                font-size: 10px;
                margin-bottom: 2px;
            `);
            keyEl.textContent = label;
            const valueEl = style(document.createElement("div"), `
                color: ${CHROME.text};
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
                border: 1px solid ${CHROME.borderSoft};
                color: #c9d0d6;
            `);
            chip.textContent = text;
            this._countsEl.appendChild(chip);
        }

        this._actionRowEl.innerHTML = "";
        const openBtn = style(document.createElement("button"), `
            ${buttonStyle(state.isFullscreenOpen ? "subtle" : "primary")}
            cursor: ${state.projectDir && !state.isFullscreenOpen ? "pointer" : "default"};
        `);
        openBtn.textContent = state.isFullscreenOpen ? "Editor Active" : "Open Editor";
        openBtn.disabled = !state.projectDir || state.isFullscreenOpen;
        openBtn.addEventListener("click", () => this.controller.openFullscreen());
        this._actionRowEl.appendChild(openBtn);

        for (const moduleId of ["assets", "preview", "queue"]) {
            const isExpanded = state.expandedModuleId === moduleId;
            const btn = style(document.createElement("button"), `
                ${buttonStyle(isExpanded ? "active" : "muted")}
                cursor: ${state.projectDir ? "pointer" : "default"};
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
            border-top: 1px solid ${CHROME.border};
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
        if (moduleId !== "assets" && moduleId !== "preview") {
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
                border: 1px solid ${CHROME.borderSoft};
                color: ${CHROME.textDim};
            `);
            loadingEl.textContent = "Loading…";
            this._moduleContainerEl.appendChild(loadingEl);
        } else if (error) {
            const errorEl = style(document.createElement("div"), `
                padding: 10px;
                border-radius: 6px;
                background: ${CHROME.dangerSoft};
                border: 1px solid ${CHROME.dangerBorder};
                color: ${CHROME.dangerText};
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
        this._preFullscreenModuleId = "";
        this.modules = this._buildModules();
        this.card = new DormantNodeCard(this);
        this.root = this.card.getElement();
        this._height = 190;
        this._resizeScheduled = false;
        this._programmaticResize = false;
        this._destroyed = false;
        this._queueSaveCompletionCounter = 0;
        this._lastQueueSettledSaveCompletionCounter = 0;
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
            if (this.state.isFullscreenOpen) return;
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
            this._queueSaveCompletionCounter = 0;
            this._lastQueueSettledSaveCompletionCounter = 0;
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
            this._queueSaveCompletionCounter = 0;
            this._lastQueueSettledSaveCompletionCounter = 0;
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
        this._preFullscreenModuleId = this.state.expandedModuleId || "";
        if (this.state.expandedModuleId) {
            this.collapseModule();
        }
        this.state.isFullscreenOpen = true;
        this.render();

        try {
            const session = new FullscreenEditorSession(this);
            this.fullscreenSession = session;
            session.mount();
        } catch (e) {
            console.warn("[LTX Editor] Failed to open fullscreen editor:", e);
            if (this.fullscreenSession) {
                const failedSession = this.fullscreenSession;
                this.fullscreenSession = null;
                try {
                    failedSession.destroy();
                } catch (cleanupErr) {
                    console.warn("[LTX Editor] Failed to clean up fullscreen session after mount error:", cleanupErr);
                }
            }
            this.fullscreenSession = null;
            this.state.isFullscreenOpen = false;
            if (this._preFullscreenModuleId && this.modules[this._preFullscreenModuleId]) {
                this.expandModule(this._preFullscreenModuleId);
                this._preFullscreenModuleId = "";
            }
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
        const restoreModuleId = this._preFullscreenModuleId;
        this._preFullscreenModuleId = "";
        if (restoreModuleId && this.modules[restoreModuleId]) {
            this.expandModule(restoreModuleId);
        } else {
            this._reloadExpandedModuleIfNeeded(["scene", "assets", "queue"]);
        }
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
        this._queueSaveCompletionCounter += 1;
        this.syncStateFromWidgets();
        this._invalidateModules(["assets", "scene", "queue"]);
        this._reloadExpandedModuleIfNeeded(["assets", "scene", "queue"]);
        this.refreshSummary({ syncAssets: true }).finally(() => this.render());
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
    }

    async handleQueueExecutionSettled({ allowRollback = false } = {}) {
        if (this._destroyed || !this.state.projectDir) {
            return this.state.dormantSummary?.queue_counts || {};
        }
        this.syncStateFromWidgets();
        this._invalidateModules(["queue"]);
        this._reloadExpandedModuleIfNeeded(["queue"]);
        await this.refreshSummary();
        let counts = this.state.dormantSummary?.queue_counts || {};
        const sawSaveCompletion = this._queueSaveCompletionCounter > this._lastQueueSettledSaveCompletionCounter;
        if (allowRollback && (counts.running || 0) > 0 && !sawSaveCompletion) {
            const rolledBack = await this._rollbackStaleRunningQueueJobs();
            if (rolledBack) {
                this._invalidateModules(["queue"]);
                this._reloadExpandedModuleIfNeeded(["queue"]);
                await this.refreshSummary();
                counts = this.state.dormantSummary?.queue_counts || {};
            }
        }
        this._lastQueueSettledSaveCompletionCounter = this._queueSaveCompletionCounter;
        this.fullscreenSession?.refresh(["queue"]);
        this.render();
        return counts;
    }

    async _rollbackStaleRunningQueueJobs() {
        const queue = await this._loadQueueModule();
        const runningJobs = queue.filter((job) => (job?.status || "").toLowerCase() === "running" && job?.job_id);
        if (!runningJobs.length) return false;
        await Promise.all(runningJobs.map(async (job) => {
            const response = await fetch(buildQueueJobUrl(this.state.projectDir, job.job_id), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    status: "pending",
                    progress: 0.0,
                    error: "",
                    completed_at: "",
                    result_asset_id: "",
                }),
            });
            if (!response.ok) {
                throw new Error(`Failed to reconcile queue job ${job.job_id}: ${response.status}`);
            }
        }));
        return true;
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

    async _getBulkAssetUsages(assetIds) {
        if (!this.state.projectDir || !Array.isArray(assetIds) || !assetIds.length) return null;
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-usages`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds }),
        });
        if (!resp.ok) {
            throw new Error(`Bulk asset usage fetch failed: ${resp.status}`);
        }
        return await resp.json();
    }

    async _bulkMoveAssets(assetIds, folder = "") {
        if (!this.state.projectDir || !Array.isArray(assetIds) || !assetIds.length) return { updated: 0 };
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-move`), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds, folder }),
        });
        if (!resp.ok) {
            throw new Error(`Bulk asset move failed: ${resp.status}`);
        }
        const payload = await resp.json();
        this.fullscreenSession?.refresh(["assets"]);
        return payload;
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
        this._invalidateModules(["assets"]);
        this._reloadExpandedModuleIfNeeded(["assets"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        return { status: "trashed", ...(payload || {}) };
    }

    async _restoreAsset(assetId) {
        if (!this.state.projectDir || !assetId) return { status: "noop" };

        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/restore`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_id: assetId }),
        });
        if (!resp.ok) {
            throw new Error(`Asset restore failed: ${resp.status}`);
        }

        const payload = await resp.json();
        this._invalidateModules(["assets"]);
        this._reloadExpandedModuleIfNeeded(["assets"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        return { status: "restored", ...(payload || {}) };
    }

    async _bulkRestoreAssets(assetIds) {
        if (!this.state.projectDir || !Array.isArray(assetIds) || !assetIds.length) return { status: "noop" };
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-restore`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds }),
        });
        if (!resp.ok) {
            throw new Error(`Bulk asset restore failed: ${resp.status}`);
        }

        const payload = await resp.json();
        this._invalidateModules(["assets"]);
        this._reloadExpandedModuleIfNeeded(["assets"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        return { status: "restored", ...(payload || {}) };
    }

    async _bulkDeleteAssets(assetIds, force = false) {
        if (!this.state.projectDir || !Array.isArray(assetIds) || !assetIds.length) return { status: "noop" };
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-delete`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds, force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Bulk asset delete failed: ${resp.status}`);
        }

        const payload = await resp.json();
        this._invalidateModules(["assets"]);
        this._reloadExpandedModuleIfNeeded(["assets"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        return { status: "trashed", ...(payload || {}) };
    }

    async _permanentDeleteAsset(assetId, force = false) {
        if (!this.state.projectDir || !assetId) return { status: "noop" };

        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/permanent`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_id: assetId, force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Permanent asset delete failed: ${resp.status}`);
        }

        const payload = await resp.json();
        this._invalidateModules(["assets", "scene", "queue"]);
        this._reloadExpandedModuleIfNeeded(["assets", "scene", "queue"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        return { status: "deleted", ...(payload || {}) };
    }

    async _bulkPermanentDeleteAssets(assetIds, force = false) {
        if (!this.state.projectDir || !Array.isArray(assetIds) || !assetIds.length) return { status: "noop" };
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-permanent-delete`), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ asset_ids: assetIds, force: !!force }),
        });
        if (resp.status === 409) {
            const payload = await resp.json();
            return { status: "conflict", ...(payload || {}) };
        }
        if (!resp.ok) {
            throw new Error(`Bulk permanent asset delete failed: ${resp.status}`);
        }

        const payload = await resp.json();
        this._invalidateModules(["assets", "scene", "queue"]);
        this._reloadExpandedModuleIfNeeded(["assets", "scene", "queue"]);
        await this.refreshSummary({ syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        return { status: "deleted", ...(payload || {}) };
    }

    async _emptyTrash() {
        if (!this.state.projectDir) return { status: "noop" };
        const resp = await fetch(api.apiURL(`/ltx-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/empty-trash`), {
            method: "POST",
        });
        if (!resp.ok) {
            throw new Error(`Empty trash failed: ${resp.status}`);
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
                label: "Viewport Preview",
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

        const durationFrames = Math.max(
            0,
            parseInt(scene?.duration_frames, 10)
                || parseInt(summary?.active_scene?.duration_frames, 10)
                || 0
        );
        const initialFrame = clamp(
            parseInt(this.state.selectionStart, 10)
                || parseInt(summary?.active_scene?.selection?.generation_start_frame, 10)
                || 0,
            0,
            durationFrames
        );
        return {
            kind: "viewport",
            label: "Viewport Preview",
            subtitle: summary?.active_scene?.name || "Scene",
            scene,
            assets,
            fps: Math.max(
                1,
                Number(scene?.fps)
                    || Number(summary?.active_scene?.effective_fps)
                    || Number(summary?.fps)
                    || 24
            ),
            initialFrame,
            durationFrames,
            frameWidth: Math.max(
                1,
                parseInt(scene?.width, 10)
                    || parseInt(summary?.active_scene?.effective_width, 10)
                    || 768
            ),
            frameHeight: Math.max(
                1,
                parseInt(scene?.height, 10)
                    || parseInt(summary?.active_scene?.effective_height, 10)
                    || 512
            ),
        };
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
            onGetBulkAssetUsages: async (assetIds) => await this._getBulkAssetUsages(assetIds),
            onDeleteAsset: async (assetId, force) => await this._deleteAsset(assetId, force),
            onBulkMoveAssets: async (assetIds, folder) => await this._bulkMoveAssets(assetIds, folder),
            onBulkDeleteAssets: async (assetIds, force) => await this._bulkDeleteAssets(assetIds, force),
            onRestoreAsset: async (assetId) => await this._restoreAsset(assetId),
            onBulkRestoreAssets: async (assetIds) => await this._bulkRestoreAssets(assetIds),
            onPermanentDeleteAsset: async (assetId, force) => await this._permanentDeleteAsset(assetId, force),
            onBulkPermanentDeleteAssets: async (assetIds, force) => await this._bulkPermanentDeleteAssets(assetIds, force),
            onEmptyTrash: async () => await this._emptyTrash(),
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
        const previousOverflowX = container.style.overflowX;
        const previousOverflowY = container.style.overflowY;
        container.style.overflowX = "hidden";
        container.style.overflowY = "hidden";

        const wrap = style(document.createElement("div"), `
            display: flex;
            flex-direction: column;
            gap: 8px;
            flex: 1 1 auto;
            min-height: 0;
            overflow-y: auto;
            overflow-x: hidden;
            padding-right: 2px;
        `);
        container.appendChild(wrap);

        const header = style(document.createElement("div"), `
            display: flex;
            flex-direction: column;
            gap: 2px;
            flex: 0 0 auto;
        `);
        const title = style(document.createElement("div"), `
            color: ${CHROME.text};
            font-size: 11px;
            font-weight: 600;
        `);
        title.textContent = data.label || "Preview";
        const subtitle = style(document.createElement("div"), `
            color: ${CHROME.textDim};
            font-size: 10px;
            word-break: break-word;
        `);
        subtitle.textContent = data.subtitle || "";
        header.append(title, subtitle);
        wrap.appendChild(header);

        const surface = style(document.createElement("div"), `
            flex: 1 1 110px;
            min-height: 96px;
            border-radius: 6px;
            border: 1px solid ${CHROME.border};
            background: ${CHROME.panelMuted};
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
        `);
        wrap.appendChild(surface);

        if (data.kind === "empty") {
            const emptyEl = style(document.createElement("div"), `
                color: ${CHROME.textDim};
                font-size: 10px;
                padding: 14px;
                text-align: center;
            `);
            emptyEl.textContent = data.subtitle || "No preview available.";
            surface.appendChild(emptyEl);
            return () => {
                container.style.overflowX = previousOverflowX;
                container.style.overflowY = previousOverflowY;
            };
        }

        const stage = style(document.createElement("div"), `
            width: 100%;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 8px;
            box-sizing: border-box;
            position: relative;
            overflow: hidden;
        `);
        const canvas = style(document.createElement("canvas"), `
            display: block;
            background: #000;
            border-radius: 6px;
            box-shadow: 0 10px 22px rgba(0,0,0,0.28);
        `);
        stage.appendChild(canvas);
        surface.appendChild(stage);

        const transport = style(document.createElement("div"), `
            display: flex;
            flex-direction: column;
            gap: 8px;
            flex: 0 0 auto;
        `);
        wrap.appendChild(transport);

        const buttonRow = style(document.createElement("div"), `
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        `);
        transport.appendChild(buttonRow);

        const playBtn = style(document.createElement("button"), buttonStyle("subtle", {
            padding: "5px 10px",
            fontSize: "10px",
        }));
        playBtn.type = "button";
        playBtn.style.minWidth = "48px";

        const audioBtn = style(document.createElement("button"), buttonStyle("muted", {
            padding: "5px 10px",
            fontSize: "10px",
        }));
        audioBtn.type = "button";
        audioBtn.style.minWidth = "64px";

        const modeLabel = style(document.createElement("span"), `
            color: ${CHROME.textDim};
            font-size: 10px;
            margin-left: auto;
            white-space: nowrap;
        `);
        buttonRow.append(playBtn, audioBtn, modeLabel);

        const scrubRow = style(document.createElement("div"), `
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        `);
        const scrubber = document.createElement("input");
        scrubber.type = "range";
        scrubber.min = "0";
        scrubber.step = "1";
        scrubber.style.cssText = `
            flex: 1 1 140px;
            min-width: 120px;
            accent-color: #6fa7d8;
        `;
        const frameLabel = style(document.createElement("div"), `
            color: #a9bccb;
            font-size: 10px;
            min-width: 92px;
            text-align: right;
            white-space: nowrap;
            font-family: monospace;
            margin-left: auto;
        `);
        scrubRow.append(scrubber, frameLabel);
        transport.appendChild(scrubRow);

        const projectDir = this.state.projectDir;
        const assetsByPath = new Map((data.assets || []).map((asset) => [asset.path, asset]));
        const effectiveFps = Math.max(1, Number(data.fps) || 24);
        const totalFrames = Math.max(0, parseInt(data.durationFrames, 10) || 0);
        const lastRenderableFrame = Math.max(0, totalFrames - 1);
        const hasSceneAudio = (data.scene?.audio_tracks || []).some((track) => {
            if (track?.muted) return false;
            if (isAudioLaneHidden(data.scene, track.lane_index || 0)) return false;
            return !assetsByPath.get(track.source_path)?.missing;
        });

        let currentFrame = clamp(parseInt(data.initialFrame, 10) || 0, 0, totalFrames);
        let destroyed = false;
        let isPlaying = false;
        let audioEnabled = hasSceneAudio;
        let playbackRaf = 0;
        let renderRaf = 0;
        let pendingRenderForceSeek = false;
        let playbackStartTs = 0;
        let playbackStartFrame = currentFrame;
        let currentTarget = null;
        let activeVideoKeys = new Set();
        let activeAudioKeys = new Set();
        const previewImageCache = new Map();
        const videoEntries = new Map();
        const audioEntries = new Map();

        const clipPlaybackKey = (clip) => clip?.clip_id || `${clip?.source_path || ""}:${clip?.timeline_start_frame || 0}:${clip?.track_index || 0}`;
        const audioPlaybackKey = (track) => track?.track_id || `${track?.source_path || ""}:${track?.timeline_start_frame || 0}:${track?.lane_index || 0}`;
        const renderFrameIndex = () => (totalFrames > 0 ? clamp(currentFrame, 0, lastRenderableFrame) : 0);
        const getTargetForFrame = (frame) => pickPreviewTargetForFrame(
            projectDir,
            data.scene,
            data.assets,
            frame,
            {
                width: data.frameWidth,
                height: data.frameHeight,
            }
        );
        const getTargetForCurrentFrame = () => getTargetForFrame(renderFrameIndex());

        const consumePointerOnly = (event) => consumeDormantPointer(event);
        stage.addEventListener("pointerdown", consumePointerOnly);
        stage.addEventListener("mousedown", consumePointerOnly);
        playBtn.addEventListener("pointerdown", consumePointerOnly);
        playBtn.addEventListener("mousedown", consumePointerOnly);
        audioBtn.addEventListener("pointerdown", consumePointerOnly);
        audioBtn.addEventListener("mousedown", consumePointerOnly);
        scrubber.addEventListener("pointerdown", consumePointerOnly);
        scrubber.addEventListener("mousedown", consumePointerOnly);

        function getTargetLayers(target) {
            if (!target) return [];
            if (target.kind === "video") {
                return [{
                    key: target.key || clipPlaybackKey(target.clip),
                    clip: target.clip,
                    sourcePath: target.clip?.source_path || "",
                    mediaUrl: target.mediaUrl || buildProjectAssetViewURL(projectDir, target.clip?.source_path || ""),
                    posterUrl: target.posterUrl || null,
                    opacity: Math.max(0, Math.min(1, Number(target.clip?.opacity ?? 1))),
                    missing: false,
                }];
            }
            if (target.kind === "composite") {
                return (target.layers || []).filter(layer => layer?.clip);
            }
            return [];
        }

        function getAudioTracksAtFrame(frame) {
            return (data.scene?.audio_tracks || [])
                .filter(track => frame >= track.timeline_start_frame && frame < track.timeline_end_frame)
                .filter(track => !track.muted)
                .filter(track => !isAudioLaneHidden(data.scene, track.lane_index || 0))
                .filter(track => !assetsByPath.get(track.source_path)?.missing)
                .sort((a, b) => (a.lane_index || 0) - (b.lane_index || 0));
        }

        function clampMediaTime(mediaEl, seconds) {
            const raw = Math.max(0, Number(seconds) || 0);
            const duration = Number(mediaEl?.duration);
            if (Number.isFinite(duration) && duration > 0) {
                return clamp(raw, 0, duration);
            }
            return raw;
        }

        function sourceTimeForClip(clip, frame) {
            const sourceFrame = Math.max(
                0,
                frame - (parseInt(clip?.timeline_start_frame, 10) || 0) + (parseInt(clip?.source_in_frame, 10) || 0)
            );
            return sourceFrame / effectiveFps;
        }

        function sourceTimeForAudio(track, frame) {
            const sourceFrame = Math.max(
                0,
                frame - (parseInt(track?.timeline_start_frame, 10) || 0) + (parseInt(track?.source_in_frame, 10) || 0)
            );
            return sourceFrame / effectiveFps;
        }

        function ensureVideoEntry(layer) {
            const key = layer?.key || clipPlaybackKey(layer?.clip);
            if (!key || !layer?.sourcePath) return null;
            const existing = videoEntries.get(key);
            if (existing) {
                existing.layer = layer;
                return existing;
            }

            const video = document.createElement("video");
            video.preload = "auto";
            video.muted = true;
            video.playsInline = true;

            const entry = {
                key,
                layer,
                el: video,
                pendingTime: 0,
                cleanupHandle: null,
                listeners: [],
                cleanup: null,
            };

            const onReady = () => {
                if (destroyed) return;
                if (isPlaying && activeVideoKeys.has(key)) {
                    const desiredTime = clampMediaTime(
                        video,
                        entry.pendingTime || sourceTimeForClip(entry.layer?.clip, renderFrameIndex())
                    );
                    try {
                        if (Math.abs((video.currentTime || 0) - desiredTime) > 0.04) {
                            video.currentTime = desiredTime;
                        }
                    } catch (error) {}
                    video.muted = true;
                    video.play().catch(() => {});
                }
                scheduleRender();
            };

            for (const eventName of ["loadedmetadata", "loadeddata", "canplay", "seeked", "error"]) {
                video.addEventListener(eventName, onReady);
                entry.listeners.push([eventName, onReady]);
            }

            entry.cleanupHandle = loadMediaAsBlob(
                layer.mediaUrl || buildProjectAssetViewURL(projectDir, layer.sourcePath),
                video
            );
            entry.cleanup = () => {
                video.pause();
                entry.cleanupHandle?.cleanup?.();
                for (const [eventName, listener] of entry.listeners) {
                    video.removeEventListener(eventName, listener);
                }
                video.removeAttribute("src");
                try {
                    video.load();
                } catch (error) {}
            };

            videoEntries.set(key, entry);
            return entry;
        }

        function ensureAudioEntry(track) {
            const key = audioPlaybackKey(track);
            if (!key || !track?.source_path) return null;
            const existing = audioEntries.get(key);
            if (existing) {
                existing.track = track;
                return existing;
            }

            const audio = document.createElement("audio");
            audio.preload = "auto";

            const entry = {
                key,
                track,
                el: audio,
                pendingTime: 0,
                cleanupHandle: null,
                listeners: [],
                cleanup: null,
            };

            const onReady = () => {
                if (destroyed || !isPlaying || !audioEnabled || !activeAudioKeys.has(key)) return;
                const desiredTime = clampMediaTime(
                    audio,
                    entry.pendingTime || sourceTimeForAudio(entry.track, renderFrameIndex())
                );
                try {
                    if (Math.abs((audio.currentTime || 0) - desiredTime) > 0.04) {
                        audio.currentTime = desiredTime;
                    }
                } catch (error) {}
                audio.volume = clamp(Number(entry.track?.volume ?? 1), 0, 1);
                audio.play().catch(() => {});
            };

            for (const eventName of ["loadedmetadata", "loadeddata", "canplay", "error"]) {
                audio.addEventListener(eventName, onReady);
                entry.listeners.push([eventName, onReady]);
            }

            entry.cleanupHandle = loadMediaAsBlob(
                buildProjectAssetViewURL(projectDir, track.source_path),
                audio
            );
            entry.cleanup = () => {
                audio.pause();
                entry.cleanupHandle?.cleanup?.();
                for (const [eventName, listener] of entry.listeners) {
                    audio.removeEventListener(eventName, listener);
                }
                audio.removeAttribute("src");
                try {
                    audio.load();
                } catch (error) {}
            };

            audioEntries.set(key, entry);
            return entry;
        }

        function pauseAllVideos() {
            for (const entry of videoEntries.values()) {
                entry.el.pause();
            }
            activeVideoKeys = new Set();
        }

        function pauseAllAudios() {
            for (const entry of audioEntries.values()) {
                entry.el.pause();
            }
            activeAudioKeys = new Set();
        }

        function cleanupAllMedia() {
            pauseAllVideos();
            pauseAllAudios();
            for (const entry of videoEntries.values()) {
                entry.cleanup?.();
            }
            for (const entry of audioEntries.values()) {
                entry.cleanup?.();
            }
            videoEntries.clear();
            audioEntries.clear();
        }

        function drawGuideBackground(target) {
            if (!target?.guide?.posterUrl) return false;
            const guideImage = loadDormantPreviewImage(previewImageCache, target.guide.posterUrl, () => scheduleRender());
            return !!guideImage && drawDormantCanvasMedia(canvas, guideImage);
        }

        function drawLayerPoster(layer) {
            if (!layer?.posterUrl) return false;
            const poster = loadDormantPreviewImage(previewImageCache, layer.posterUrl, () => scheduleRender());
            return !!poster && drawDormantCanvasMedia(canvas, poster, { opacity: layer.opacity ?? 1 });
        }

        function updateTransport() {
            scrubber.max = String(totalFrames);
            scrubber.value = String(currentFrame);
            frameLabel.textContent = `f${currentFrame} / ${totalFrames}`;
            playBtn.textContent = isPlaying ? "Pause" : "Play";
            playBtn.disabled = totalFrames <= 0;
            audioBtn.textContent = hasSceneAudio ? (audioEnabled ? "Audio On" : "Muted") : "No audio";
            audioBtn.disabled = !hasSceneAudio;
            const targetKind = currentTarget?.kind || data.kind;
            if (targetKind === "video") {
                modeLabel.textContent = isPlaying ? "Playback" : "Video frame";
            } else if (targetKind === "composite") {
                modeLabel.textContent = isPlaying ? "Composite playback" : "Composite preview";
            } else if (targetKind === "image") {
                modeLabel.textContent = "Guide preview";
            } else if (targetKind === "missing") {
                modeLabel.textContent = "Missing media";
            } else {
                modeLabel.textContent = "No preview";
            }
            const targetSubtitle = currentTarget?.subtitle ? ` - ${currentTarget.subtitle}` : ` - Frame ${renderFrameIndex()}`;
            subtitle.textContent = `${data.subtitle || "Scene"}${targetSubtitle}`;
        }

        function resizeCanvas() {
            const rect = surface.getBoundingClientRect();
            const availableWidth = Math.max(1, Math.floor(rect.width - 16));
            const availableHeight = Math.max(1, Math.floor(rect.height - 16));
            if (availableWidth <= 0 || availableHeight <= 0) return;
            const aspect = data.frameWidth / Math.max(1, data.frameHeight);
            let canvasWidth = availableWidth;
            let canvasHeight = Math.floor(canvasWidth / Math.max(aspect, 0.01));
            if (canvasHeight > availableHeight) {
                canvasHeight = availableHeight;
                canvasWidth = Math.floor(canvasHeight * aspect);
            }
            canvas.width = Math.max(1, canvasWidth);
            canvas.height = Math.max(1, canvasHeight);
            canvas.style.width = `${canvasWidth}px`;
            canvas.style.height = `${canvasHeight}px`;
        }

        function scheduleRender({ forceSeek = false } = {}) {
            if (destroyed) return;
            pendingRenderForceSeek = pendingRenderForceSeek || !!forceSeek;
            if (renderRaf) return;
            renderRaf = requestAnimationFrame(() => {
                renderRaf = 0;
                const shouldForceSeek = pendingRenderForceSeek;
                pendingRenderForceSeek = false;
                renderPreviewFrame({ forceSeek: shouldForceSeek });
            });
        }

        function renderStaticTarget(target) {
            clearDormantCanvas(canvas);
            if (target.kind === "missing") {
                drawDormantCanvasMessage(canvas, target.label || "Missing media", target.subtitle || "");
                return;
            }
            if (target.kind === "empty") {
                drawDormantCanvasMessage(canvas, target.label || "No preview", target.subtitle || "");
                return;
            }
            if (target.kind === "image") {
                const image = loadDormantPreviewImage(previewImageCache, target.posterUrl, () => scheduleRender());
                if (image && drawDormantCanvasMedia(canvas, image)) {
                    return;
                }
                drawDormantCanvasMessage(canvas, "Loading guide...", target.subtitle || "");
                return;
            }
            drawDormantCanvasMessage(canvas, target.label || "Preview unavailable", target.subtitle || "");
        }

        function syncVideoPlayback(target, frame) {
            const layers = getTargetLayers(target);
            const nextKeys = new Set(layers.map(layer => layer.key || clipPlaybackKey(layer.clip)));

            for (const [key, entry] of videoEntries) {
                if (activeVideoKeys.has(key) && !nextKeys.has(key)) {
                    entry.el.pause();
                }
            }

            for (const layer of layers) {
                const entry = ensureVideoEntry(layer);
                if (!entry) continue;
                entry.layer = layer;
                entry.pendingTime = sourceTimeForClip(layer.clip, frame);
                const video = entry.el;
                if (video.readyState >= 2) {
                    const desiredTime = clampMediaTime(video, entry.pendingTime);
                    if (!activeVideoKeys.has(entry.key) || Math.abs((video.currentTime || 0) - desiredTime) > 0.25) {
                        try {
                            video.currentTime = desiredTime;
                        } catch (error) {}
                    }
                    video.muted = true;
                    if (video.paused) {
                        video.play().catch(() => {});
                    }
                }
            }

            activeVideoKeys = nextKeys;
        }

        function syncAudioPlayback(frame) {
            if (!audioEnabled) {
                pauseAllAudios();
                return;
            }

            const tracks = getAudioTracksAtFrame(frame);
            const nextKeys = new Set(tracks.map(audioPlaybackKey));

            for (const [key, entry] of audioEntries) {
                if (activeAudioKeys.has(key) && !nextKeys.has(key)) {
                    entry.el.pause();
                }
            }

            for (const track of tracks) {
                const entry = ensureAudioEntry(track);
                if (!entry) continue;
                entry.track = track;
                entry.pendingTime = sourceTimeForAudio(track, frame);
                const audio = entry.el;
                audio.volume = clamp(Number(track.volume ?? 1), 0, 1);
                if (audio.readyState >= 2) {
                    const desiredTime = clampMediaTime(audio, entry.pendingTime);
                    if (!activeAudioKeys.has(entry.key) || audio.paused || Math.abs((audio.currentTime || 0) - desiredTime) > 0.25) {
                        try {
                            audio.currentTime = desiredTime;
                        } catch (error) {}
                        audio.play().catch(() => {});
                    }
                }
            }

            activeAudioKeys = nextKeys;
        }

        function renderVideoTarget(target, { forceSeek = false } = {}) {
            clearDormantCanvas(canvas);
            let drewAny = drawGuideBackground(target);
            const frame = renderFrameIndex();
            const layers = getTargetLayers(target);

            if (!layers.length) {
                if (!drewAny) {
                    drawDormantCanvasMessage(canvas, target.label || "No preview", target.subtitle || "");
                }
                return;
            }

            for (const layer of layers) {
                const entry = ensureVideoEntry(layer);
                if (!entry) {
                    drewAny = drawLayerPoster(layer) || drewAny;
                    continue;
                }

                entry.layer = layer;
                entry.pendingTime = sourceTimeForClip(layer.clip, frame);
                const video = entry.el;
                const desiredTime = clampMediaTime(video, entry.pendingTime);

                if (isPlaying) {
                    if (video.readyState >= 2) {
                        const drift = Math.abs((video.currentTime || 0) - desiredTime);
                        if (forceSeek || drift > 0.25) {
                            try {
                                video.currentTime = desiredTime;
                            } catch (error) {}
                        }
                        video.muted = true;
                        if (video.paused) {
                            video.play().catch(() => {});
                        }
                        if (!forceSeek && !video.seeking) {
                            drewAny = drawDormantCanvasMedia(canvas, video, { opacity: layer.opacity ?? 1 }) || drewAny;
                        } else {
                            drewAny = drawLayerPoster(layer) || drewAny;
                        }
                    } else {
                        drewAny = drawLayerPoster(layer) || drewAny;
                    }
                } else {
                    video.pause();
                    if (video.readyState >= 2) {
                        const settled = !forceSeek && !video.seeking && Math.abs((video.currentTime || 0) - desiredTime) < 0.04;
                        if (!settled) {
                            try {
                                video.currentTime = desiredTime;
                            } catch (error) {}
                        }
                        if (settled) {
                            drewAny = drawDormantCanvasMedia(canvas, video, { opacity: layer.opacity ?? 1 }) || drewAny;
                        } else {
                            drewAny = drawLayerPoster(layer) || drewAny;
                        }
                    } else {
                        drewAny = drawLayerPoster(layer) || drewAny;
                    }
                }
            }

            if (!drewAny) {
                drawDormantCanvasMessage(
                    canvas,
                    isPlaying ? "Loading video..." : "Seeking frame...",
                    target.subtitle || ""
                );
            }
        }

        function renderPreviewFrame({ forceSeek = false } = {}) {
            if (destroyed) return;
            resizeCanvas();
            currentTarget = getTargetForCurrentFrame();

            if (isPlaying) {
                if (currentTarget?.kind === "video" || currentTarget?.kind === "composite") {
                    syncVideoPlayback(currentTarget, renderFrameIndex());
                } else {
                    pauseAllVideos();
                }
                syncAudioPlayback(renderFrameIndex());
            } else {
                pauseAllVideos();
                pauseAllAudios();
            }

            if (currentTarget?.kind === "video" || currentTarget?.kind === "composite") {
                renderVideoTarget(currentTarget, { forceSeek });
            } else {
                renderStaticTarget(currentTarget || { kind: "empty", label: "No preview", subtitle: "" });
            }
            updateTransport();
        }

        const resizeObserver = new ResizeObserver(() => scheduleRender());
        resizeObserver.observe(surface);

        function stopPlayback({ preservePlayhead = true, shouldRender = true } = {}) {
            if (!preservePlayhead) {
                currentFrame = 0;
            }
            if (!isPlaying && !shouldRender) return;
            isPlaying = false;
            if (playbackRaf) {
                cancelAnimationFrame(playbackRaf);
                playbackRaf = 0;
            }
            pauseAllVideos();
            pauseAllAudios();
            if (shouldRender) {
                scheduleRender({ forceSeek: true });
            }
            updateTransport();
        }

        function startPlayback() {
            if (destroyed || totalFrames <= 0) return;
            if (currentFrame >= totalFrames) {
                currentFrame = 0;
            }
            isPlaying = true;
            playbackStartTs = performance.now();
            playbackStartFrame = renderFrameIndex();
            currentFrame = playbackStartFrame;
            const tick = (timestamp) => {
                if (!isPlaying || destroyed) return;
                const elapsedFrames = Math.floor(((timestamp - playbackStartTs) / 1000) * effectiveFps);
                currentFrame = clamp(playbackStartFrame + elapsedFrames, 0, totalFrames);
                renderPreviewFrame();
                if (currentFrame >= totalFrames) {
                    stopPlayback({ preservePlayhead: true, shouldRender: true });
                    return;
                }
                playbackRaf = requestAnimationFrame(tick);
            };
            scheduleRender({ forceSeek: true });
            updateTransport();
            playbackRaf = requestAnimationFrame(tick);
        }

        playBtn.addEventListener("click", (event) => {
            consumeDormantPointer(event, { preventDefault: true });
            if (isPlaying) {
                stopPlayback({ preservePlayhead: true, shouldRender: true });
                return;
            }
            startPlayback();
        });

        audioBtn.addEventListener("click", (event) => {
            consumeDormantPointer(event, { preventDefault: true });
            if (!hasSceneAudio) return;
            audioEnabled = !audioEnabled;
            if (!audioEnabled) {
                pauseAllAudios();
            } else if (isPlaying) {
                syncAudioPlayback(renderFrameIndex());
            }
            updateTransport();
        });

        scrubber.addEventListener("input", (event) => {
            consumeDormantPointer(event);
            if (isPlaying) {
                stopPlayback({ preservePlayhead: true, shouldRender: false });
            }
            currentFrame = clamp(parseInt(scrubber.value, 10) || 0, 0, totalFrames);
            scheduleRender({ forceSeek: true });
            updateTransport();
        });

        updateTransport();
        scheduleRender({ forceSeek: true });

        return () => {
            destroyed = true;
            stopPlayback({ preservePlayhead: true, shouldRender: false });
            if (renderRaf) {
                cancelAnimationFrame(renderRaf);
                renderRaf = 0;
            }
            resizeObserver.disconnect();
            stage.removeEventListener("pointerdown", consumePointerOnly);
            stage.removeEventListener("mousedown", consumePointerOnly);
            playBtn.removeEventListener("pointerdown", consumePointerOnly);
            playBtn.removeEventListener("mousedown", consumePointerOnly);
            audioBtn.removeEventListener("pointerdown", consumePointerOnly);
            audioBtn.removeEventListener("mousedown", consumePointerOnly);
            scrubber.removeEventListener("pointerdown", consumePointerOnly);
            scrubber.removeEventListener("mousedown", consumePointerOnly);
            cleanupAllMedia();
            container.style.overflowX = previousOverflowX;
            container.style.overflowY = previousOverflowY;
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
                border: 1px solid ${CHROME.borderSoft};
                color: ${CHROME.textDim};
            `);
            emptyEl.textContent = "Render queue is empty.";
            wrap.appendChild(emptyEl);
            return null;
        }

        const settings = getEditorSettings();
        const timecodeMode = settings?.timelineBehavior?.timecodeMode === "timecode" ? "timecode" : "frames";
        const collapsedBatchIds = readQueueBatchCollapseState(this.state.projectDir, settings);
        const fallbackFps = Math.max(
            1,
            Number(this.state?.dormantSummary?.active_scene?.effective_fps)
                || Number(this.state?.dormantSummary?.fps)
                || 24
        );
        const colors = {
            pending: CHROME.textMuted,
            running: "#67a6d6",
            completed: "#68a376",
            failed: "#c66d76",
        };

        const createQueueRow = (job, options = {}) => {
            const row = style(document.createElement("div"), `
                display: grid;
                grid-template-columns: auto minmax(0, 1fr);
                gap: 8px;
                padding: 7px 8px${options.nested ? " 7px 18px" : ""};
                border-radius: 6px;
                background: ${options.nested ? "rgba(255,255,255,0.02)" : "rgba(255,255,255,0.03)"};
                border: 1px solid ${CHROME.borderSoft};
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
            if (job.prompt) {
                text.title = job.prompt;
            }

            const headingRow = style(document.createElement("div"), `
                display: flex;
                align-items: baseline;
                justify-content: space-between;
                gap: 8px;
                min-width: 0;
            `);

            const title = style(document.createElement("div"), `
                color: ${CHROME.text};
                font-size: 11px;
                font-weight: 600;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
                min-width: 0;
            `);
            title.textContent = options.title || job.scene_name || "Scene";

            const status = style(document.createElement("div"), `
                color: ${CHROME.textMuted};
                font-size: 10px;
                flex-shrink: 0;
                white-space: nowrap;
            `);
            status.textContent = formatQueueStatusLabel(job?.status);

            const selectionSummary = style(document.createElement("div"), `
                color: ${CHROME.textDim};
                font-size: 10px;
                line-height: 1.35;
                white-space: normal;
                overflow-wrap: anywhere;
            `);
            selectionSummary.textContent = formatQueueSelectionSummary(job, {
                fps: fallbackFps,
                mode: timecodeMode,
            });

            headingRow.append(title, status);
            text.append(headingRow, selectionSummary);
            row.append(dot, text);
            return row;
        };

        const groups = groupQueueJobs(jobs);
        for (const entry of groups) {
            if (entry.type === "single") {
                wrap.appendChild(createQueueRow(entry.job));
                continue;
            }

            const batchTotal = Math.max(
                entry.jobs.length,
                ...entry.jobs.map((job) => Math.max(0, parseInt(job?.batch_total, 10) || 0)),
            );
            const countLabel = entry.jobs.length === batchTotal
                ? `${batchTotal} chunk${batchTotal === 1 ? "" : "s"}`
                : `${entry.jobs.length} of ${batchTotal} chunks`;
            const isOpen = !collapsedBatchIds.has(entry.batchId);

            const group = style(document.createElement("div"), `
                display: flex;
                flex-direction: column;
                gap: 4px;
                padding: 0;
                border-radius: 6px;
                background: rgba(255,255,255,0.02);
                border: 1px solid ${CHROME.borderSoft};
            `);

            const header = style(document.createElement("button"), `
                display: flex;
                align-items: center;
                justify-content: space-between;
                gap: 8px;
                width: 100%;
                padding: 7px 8px;
                background: rgba(255,255,255,0.03);
                border-bottom: 1px solid ${CHROME.borderSoft};
                color: ${CHROME.text};
                font-size: 10px;
                font-weight: 700;
                cursor: pointer;
                border-top: none;
                border-left: none;
                border-right: none;
                border-bottom-left-radius: 0;
                border-bottom-right-radius: 0;
                text-align: left;
            `);
            header.type = "button";
            header.setAttribute("aria-expanded", isOpen ? "true" : "false");

            const label = style(document.createElement("span"), `
                min-width: 0;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            `);
            label.textContent = `${isOpen ? "v" : ">"} Batch ${entry.batchId.slice(0, 8)} - ${countLabel}`;

            const scene = style(document.createElement("span"), `
                color: ${CHROME.textMuted};
                font-weight: 600;
                flex-shrink: 0;
            `);
            scene.textContent = entry.jobs[0]?.scene_name || "Scene";

            header.append(label, scene);
            group.appendChild(header);
            header.addEventListener("pointerdown", (event) => consumeDormantPointer(event, { preventDefault: true }));
            header.addEventListener("mousedown", (event) => consumeDormantPointer(event, { preventDefault: true }));

            const rows = style(document.createElement("div"), `
                display: ${isOpen ? "flex" : "none"};
                flex-direction: column;
                gap: 4px;
                padding: 0 0 4px;
            `);
            entry.jobs.forEach((job, index) => {
                const chunkIndex = Math.max(1, (parseInt(job?.batch_index, 10) || index) + 1);
                rows.appendChild(createQueueRow(job, {
                    title: `Chunk ${chunkIndex} of ${batchTotal}`,
                    nested: true,
                }));
            });
            group.appendChild(rows);

            header.addEventListener("click", (event) => {
                consumeDormantPointer(event, { preventDefault: true });
                const nextOpen = rows.style.display === "none";
                rows.style.display = nextOpen ? "flex" : "none";
                header.setAttribute("aria-expanded", nextOpen ? "true" : "false");
                label.textContent = `${nextOpen ? "v" : ">"} Batch ${entry.batchId.slice(0, 8)} - ${countLabel}`;
                if (nextOpen) {
                    collapsedBatchIds.delete(entry.batchId);
                } else {
                    collapsedBatchIds.add(entry.batchId);
                }
                persistQueueBatchCollapseState(this.state.projectDir, collapsedBatchIds);
            });

            wrap.appendChild(group);
        }

        return null;
    }
}
