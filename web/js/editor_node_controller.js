const { api } = window.comfyAPI.api;

import {
    frameConstraintsEqual,
    getEditorSettings,
    resolveFrameConstraintForTemplate,
    updateEditorSettings,
} from "./editor_settings.js";
import { installProjectVersionFetchPatch, getProjectVersion } from "./api_client.js";
import {
    claimEditorSession,
    createEditorHandoff,
    getEditorSession,
    getEditorWidgetState,
    heartbeatCanvasHost,
    heartbeatEditorSession,
    newEditorSessionId,
    putEditorWidgetState,
    releaseEditorSession,
} from "./editor_session_client.js";
import { connectProjectSync } from "./cross_tab_sync.js";
import { EditorWidget, buildProjectAssetViewURL, importFileIntoProject, replaceAssetInProject } from "./editor_widget.js";
import { loadMediaAsBlob, mountSharedAssetGallery } from "./shared_asset_gallery.js";
import { deriveCurrentSceneAssetIds } from "./current_scene_assets.js";
import {
    mountSharedRenderQueue,
    persistQueueBatchCollapseState,
    readQueueBatchCollapseState,
} from "./shared_render_queue.js";
import { register as registerKeyboardConsumer, PRIORITY as KEYBOARD_PRIORITY } from "./keyboard_ownership.js";
import { notifyProgress, notifyInfo } from "./editor_notifications.js";
import { EDITOR_CHROME as CHROME, FONT, THEME, statusPillCss } from "./editor_theme.js";

// Session-diagnostic mode is gated by `window.SONDER_DEBUG_SESSION === true`.
// When off, the helpers below are no-ops with zero allocation. Do NOT enable
// minification of this module without re-validating the stack-based caller
// attribution in `_logRender` — the second frame of `new Error().stack` is
// expected to identify the immediate caller.
//
// Persistent enable across page reloads: set `localStorage.SONDER_DEBUG_SESSION = "1"`
// once in the canvas page console; this bootstrap copies it into the window
// global before the controller is constructed.
const SESSION_DIAG_RING_MAX = 2048;
const WIDGET_STATE_FALLBACK_POLL_MS = 2000;

if (typeof window !== "undefined" && !window.SONDER_DEBUG_SESSION) {
    try {
        if (window.localStorage?.getItem?.("SONDER_DEBUG_SESSION") === "1") {
            window.SONDER_DEBUG_SESSION = true;
        }
    } catch (_) {}
}

if (typeof window !== "undefined" && !window.SONDER_DEBUG_PLAYBACK_BOUNDARY) {
    try {
        if (window.localStorage?.getItem?.("SONDER_DEBUG_PLAYBACK_BOUNDARY") === "1") {
            window.SONDER_DEBUG_PLAYBACK_BOUNDARY = true;
        }
    } catch (_) {}
}

function isSessionDiagEnabled() {
    return typeof window !== "undefined" && window.SONDER_DEBUG_SESSION === true;
}

function dormantBoundaryDebugEvent(eventName, details = {}) {
    if (typeof window === "undefined" || !window.SONDER_DEBUG_PLAYBACK_BOUNDARY) return;
    console.log("[Sonder Dormant Boundary]", eventName, details);
}

function style(el, cssText) {
    el.style.cssText = cssText;
    return el;
}

function coerceBoolean(value, defaultValue = false) {
    if (typeof value === "boolean") return value;
    if (typeof value === "number") return value !== 0;
    if (typeof value === "string") {
        const normalized = value.trim().toLowerCase();
        if (["1", "true", "yes", "on"].includes(normalized)) return true;
        if (["0", "false", "no", "off"].includes(normalized)) return false;
    }
    return defaultValue;
}

const EDITOR_WIDGET_FIELDS = [
    "scene_id",
    "selection_start",
    "selection_end",
    "pre_context_frames",
    "post_context_frames",
    "mask_pre_offset",
    "mask_post_offset",
    "take_placement_mode",
    "take_placement_linked",
    "take_placement_muted",
    "render_queue_active",
];

const PREVIEW_WIDGET_FIELDS = new Set([
    "scene_id",
    "selection_start",
    "selection_end",
    "pre_context_frames",
    "post_context_frames",
    "render_queue_active",
]);
const PREVIEW_STATE_REFRESH_DEBOUNCE_MS = 80;

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
        text: CHROME.text,
    },
    active: {
        background: CHROME.accent,
        border: CHROME.accentHi,
        text: CHROME.bg,
    },
};

export function buttonStyle(variant = "muted", { padding = "6px 10px", radius = "6px", fontSize = "11px", fontWeight = "600" } = {}) {
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

function makeStatusPill(text, state = "idle", { fontSize = "10px", padding = "2px 8px" } = {}) {
    const pill = style(document.createElement("span"), `
        ${statusPillCss({ state, padding })}
        font-size: ${fontSize};
        line-height: 1.35;
        font-weight: 600;
    `);
    const dot = style(document.createElement("span"), `
        width: 6px;
        height: 6px;
        border-radius: 999px;
        background: var(--sonder-status-color);
        flex: 0 0 auto;
    `);
    const label = document.createElement("span");
    label.textContent = text;
    pill.append(dot, label);
    return pill;
}

function iconForAssetType(type) {
    if (type === "video") return "🎬";
    if (type === "image") return "🖼";
    if (type === "audio") return "🔊";
    if (type === "artifact") return "🗂";
    return "•";
}

function projectIdFromDir(projectDir) {
    return projectDir ? projectDir.split(/[/\\]/).pop() : "";
}

const CANVAS_INSTANCE_STORAGE_KEY = "sonder-editor-canvas-instance-id";

function getCanvasInstanceId() {
    const fallback = `canvas-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    try {
        const storage = globalThis.sessionStorage;
        if (!storage) return fallback;
        let value = storage.getItem(CANVAS_INSTANCE_STORAGE_KEY);
        if (!value) {
            value = globalThis.crypto?.randomUUID?.() || fallback;
            storage.setItem(CANVAS_INSTANCE_STORAGE_KEY, value);
        }
        return value;
    } catch (_err) {
        return fallback;
    }
}

function formatCountLabel(prefix, value) {
    return `${prefix}${value || 0}`;
}

function formatDurationFrames(frameCount) {
    if (!Number.isFinite(frameCount) || frameCount <= 0) return "0f";
    return `${frameCount}f`;
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

export function renderDormantMediaScrubBar(mediaEl) {
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
        background: ${THEME.bg3};
        cursor: pointer;
        overflow: hidden;
    `);
    const fill = style(document.createElement("div"), `
        position: absolute;
        left: 0;
        top: 0;
        bottom: 0;
        width: 0;
        background: linear-gradient(90deg, ${THEME.accent}, ${THEME.accentHi});
    `);
    const thumb = style(document.createElement("div"), `
        position: absolute;
        top: 50%;
        width: 12px;
        height: 12px;
        border-radius: 50%;
        background: ${THEME.fg0};
        border: 1px solid rgba(0,0,0,0.35);
        transform: translate(-50%,-50%);
        left: 100%;
        pointer-events: none;
        box-shadow: 0 1px 3px rgba(0,0,0,0.35);
    `);
    fill.appendChild(thumb);
    track.appendChild(fill);
    const label = style(document.createElement("div"), `
        color: ${THEME.fg1};
        font-size: 10px;
        white-space: nowrap;
        min-width: 72px;
        text-align: right;
    `);
    wrap.append(track, label);

    let dragging = false;
    // When the user grabs the scrubber while the media is playing, pause for the
    // duration of the drag and resume on release — otherwise active playback keeps
    // overwriting the seeked currentTime and the scrub reads as unresponsive.
    let resumeAfterDrag = false;

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
        if (resumeAfterDrag) {
            resumeAfterDrag = false;
            try {
                const resume = mediaEl?.play?.();
                if (resume?.catch) resume.catch(() => {});
            } catch (_) { /* resume is best-effort */ }
        }
    };

    track.addEventListener("mousedown", (event) => {
        event.preventDefault();
        dragging = true;
        resumeAfterDrag = !!mediaEl && !mediaEl.paused;
        if (resumeAfterDrag) {
            try { mediaEl.pause(); } catch (_) { /* pause is best-effort */ }
        }
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
            resumeAfterDrag = false;
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
    ctx.fillStyle = THEME.bg0;
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
    ctx.fillStyle = THEME.fg3;
    ctx.font = `400 ${Math.max(16, Math.floor(height / 12))}px ${FONT.mono}`;
    ctx.fillText(title, width / 2, height / 2 - (subtitle ? 12 : 0));
    if (subtitle) {
        ctx.fillStyle = THEME.fg2;
        ctx.font = `400 ${Math.max(11, Math.floor(height / 24))}px ${FONT.sans}`;
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

// Per-item fit modes mirror server/media_helpers.py and the fullscreen viewport.
// Dormant is a separate renderer by design (dormant/fullscreen separation), so the
// per-mode branch is duplicated here rather than shared. Dormant draws no scene
// outline (intentionally out of scope).
const DORMANT_FIT_MODES = new Set(["fit", "pad_edge", "cover", "stretch"]);

function dormantFitOptions(item) {
    const mode = item?.fit_mode;
    return {
        fitMode: DORMANT_FIT_MODES.has(mode) ? mode : "pad_edge",
        cropPosition: item?.crop_position || "center",
    };
}

function drawDormantCanvasMedia(canvas, media, { opacity = 1, fitMode = "pad_edge", cropPosition = "center" } = {}) {
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
    const mode = DORMANT_FIT_MODES.has(fitMode) ? fitMode : "pad_edge";
    const prevAlpha = ctx.globalAlpha;
    if (opacity < 1) ctx.globalAlpha = Math.max(0, opacity);
    try {
        if (mode === "stretch") {
            ctx.drawImage(media, 0, 0, canvasWidth, canvasHeight);
        } else if (mode === "cover") {
            const coverScale = Math.max(canvasWidth / mediaWidth, canvasHeight / mediaHeight);
            const srcW = Math.min(mediaWidth, canvasWidth / coverScale);
            const srcH = Math.min(mediaHeight, canvasHeight / coverScale);
            const xExtra = mediaWidth - srcW;
            const yExtra = mediaHeight - srcH;
            let sx = xExtra / 2;
            if (cropPosition === "left") sx = 0;
            else if (cropPosition === "right") sx = xExtra;
            let sy = yExtra / 2;
            if (cropPosition === "top") sy = 0;
            else if (cropPosition === "bottom") sy = yExtra;
            ctx.drawImage(media, sx, sy, srcW, srcH, 0, 0, canvasWidth, canvasHeight);
        } else {
            const scale = Math.min(canvasWidth / mediaWidth, canvasHeight / mediaHeight);
            const drawWidth = Math.max(1, Math.round(mediaWidth * scale));
            const drawHeight = Math.max(1, Math.round(mediaHeight * scale));
            const drawX = Math.floor((canvasWidth - drawWidth) / 2);
            const drawY = Math.floor((canvasHeight - drawHeight) / 2);
            ctx.drawImage(media, drawX, drawY, drawWidth, drawHeight);
            if (mode === "pad_edge") {
                const left = drawX;
                const top = drawY;
                const right = canvasWidth - (drawX + drawWidth);
                const bottom = canvasHeight - (drawY + drawHeight);
                if (left > 0.5) ctx.drawImage(media, 0, 0, 1, mediaHeight, 0, drawY, left, drawHeight);
                if (right > 0.5) ctx.drawImage(media, mediaWidth - 1, 0, 1, mediaHeight, drawX + drawWidth, drawY, right, drawHeight);
                if (top > 0.5) ctx.drawImage(media, 0, 0, mediaWidth, 1, drawX, 0, drawWidth, top);
                if (bottom > 0.5) ctx.drawImage(media, 0, mediaHeight - 1, mediaWidth, 1, drawX, drawY + drawHeight, drawWidth, bottom);
            }
        }
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
    return api.apiURL(`/sonder-editor/project/${projectId}/dormant_summary?${params.toString()}`);
}

function buildDormantAssetsUrl(projectDir) {
    return api.apiURL(`/sonder-editor/project/${projectIdFromDir(projectDir)}/assets/dormant?include_trashed=true`);
}

function buildQueueUrl(projectDir) {
    return api.apiURL(`/sonder-editor/project/${projectIdFromDir(projectDir)}/queue`);
}

function buildQueueJobUrl(projectDir, jobId) {
    return api.apiURL(`/sonder-editor/project/${projectIdFromDir(projectDir)}/queue/${jobId}`);
}

function buildSceneUrl(projectDir, sceneId) {
    return api.apiURL(`/sonder-editor/project/${projectIdFromDir(projectDir)}/scenes/${sceneId}`);
}

function buildProjectUrl(projectDir) {
    return api.apiURL(`/sonder-editor/project/${encodeURIComponent(projectIdFromDir(projectDir))}`);
}

async function healProjectFrameConstraint(projectDir, settings = getEditorSettings()) {
    if (!projectDir) return;
    const resp = await fetch(buildProjectUrl(projectDir));
    if (!resp.ok) {
        throw new Error(`Project fetch failed: ${resp.status}`);
    }
    const project = await resp.json();
    const expected = resolveFrameConstraintForTemplate(project?.template_id, settings);
    if (frameConstraintsEqual(expected, project?.frame_constraint)) return;
    const updateResp = await fetch(buildProjectUrl(projectDir), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ frame_constraint: expected }),
    });
    if (!updateResp.ok) {
        throw new Error(`Frame-constraint self-heal failed: ${updateResp.status}`);
    }
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

function isGuideTrackHidden(scene) {
    return !!scene?.guide_track_config?.hidden;
}

function pickPreviewTargetForFrame(projectDir, scene, assets, frame, fallbackDimensions = {}) {
    const fallbackFrame = Math.max(0, parseInt(frame, 10) || 0);
    const frameWidth = Math.max(0, parseInt(scene?.width, 10) || parseInt(fallbackDimensions.width, 10) || 0);
    const frameHeight = Math.max(0, parseInt(scene?.height, 10) || parseInt(fallbackDimensions.height, 10) || 0);
    const assetsByPath = new Map((assets || []).map(asset => [asset.path, asset]));
    const assetsById = new Map((assets || []).map(asset => [asset.asset_id, asset]));
    const isMissingAsset = (asset) => !asset || !!asset.missing;

    const activeClipRecords = (scene?.clips || [])
        .map((clip, index) => ({ clip, index }))
        .filter(({ clip }) => fallbackFrame >= clip.timeline_start_frame && fallbackFrame < clip.timeline_end_frame)
        .filter(({ clip }) => !clip.muted)
        .filter(({ clip }) => !clip.role || clip.role === "render")
        .filter(({ clip }) => !isVideoLaneHidden(scene, clip.track_index || 0));
    const activeClipByLane = new Map();
    for (const record of activeClipRecords) {
        const laneIndex = record.clip?.track_index || 0;
        const current = activeClipByLane.get(laneIndex);
        if (!current) {
            activeClipByLane.set(laneIndex, record);
            continue;
        }
        const nextStart = parseInt(record.clip?.timeline_start_frame, 10) || 0;
        const currentStart = parseInt(current.clip?.timeline_start_frame, 10) || 0;
        const nextEnd = parseInt(record.clip?.timeline_end_frame, 10) || 0;
        const currentEnd = parseInt(current.clip?.timeline_end_frame, 10) || 0;
        if (
            nextStart > currentStart
            || (nextStart === currentStart && nextEnd > currentEnd)
            || (nextStart === currentStart && nextEnd === currentEnd && record.index > current.index)
        ) {
            activeClipByLane.set(laneIndex, record);
        }
    }
    const activeClips = Array.from(activeClipByLane.values())
        .sort((a, b) => {
            const trackDelta = (b.clip?.track_index || 0) - (a.clip?.track_index || 0);
            if (trackDelta) return trackDelta;
            const startDelta = (b.clip?.timeline_start_frame || 0) - (a.clip?.timeline_start_frame || 0);
            if (startDelta) return startDelta;
            return b.index - a.index;
        })
        .map(({ clip }) => clip);

    let guide = null;
    let guideFrame = -1;
    if (!isGuideTrackHidden(scene)) {
        for (const item of (scene?.guide_frames || [])) {
            if (item.muted) continue;
            const frameIndex = item.frame_index === -1
                ? Math.max(0, (scene?.duration_frames || 1) - 1)
                : item.frame_index;
            if (frameIndex <= fallbackFrame && frameIndex >= guideFrame) {
                guide = item;
                guideFrame = frameIndex;
            }
        }
    }
    const guideAsset = guide ? assetsById.get(guide.asset_id) : null;
    const guidePreview = guideAsset && !isMissingAsset(guideAsset)
        ? {
            posterUrl: buildProjectAssetViewURL(projectDir, guideAsset.path),
            fit_mode: guide?.fit_mode,
            crop_position: guide?.crop_position,
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
                        ? api.apiURL(`/sonder-editor/project/${projectIdFromDir(projectDir)}/thumbnail/${asset.asset_id}`)
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
                ? api.apiURL(`/sonder-editor/project/${projectIdFromDir(projectDir)}/thumbnail/${asset.asset_id}`)
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
                fit_mode: guide?.fit_mode,
                crop_position: guide?.crop_position,
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
        this._destroyPromise = null;
    }

    mount() {
        if (this._destroyed || !this.controller.state.projectDir) return;

        const state = this.controller.state;
        const editor = new EditorWidget(this.controller.node, {
            onFullscreenExit: () => this._handleEditorClosed(),
            onMountInTab: () => this.controller.mountFullscreenInTab(),
            onWidgetValueChange: (name, value) => this.controller.onEditorWidgetValueChange(name, value),
        });

        this.editor = editor;
        editor.renderQueueActive = coerceBoolean(state.renderQueueActive, true);
        editor._setWidgetValue("render_queue_active", editor.renderQueueActive);
        editor.updateProject(state.projectDir);
        editor.activeSceneId = state.sceneId || "";
        editor.selectionStart = state.selectionStart || 0;
        editor.selectionEnd = state.selectionEnd || 0;
        editor.playhead = state.selectionStart || 0;
        this.controller._setWidgetValue("pre_context_frames", state.preContextFrames || 0);
        this.controller._setWidgetValue("post_context_frames", state.postContextFrames || 0);
        this.controller._setWidgetValue("mask_pre_offset", state.maskPreOffset || 0);
        this.controller._setWidgetValue("mask_post_offset", state.maskPostOffset || 0);
        editor._refreshContextInputs();
        editor.refresh(["queue"]);
        editor._enterFullscreen();
    }

    refresh(keys = []) {
        this.editor?.refresh(keys);
    }

    _handleEditorClosed() {
        void this.destroy(true);
    }

    destroy(fromEditor = false) {
        if (this._destroyed) return this._destroyPromise || Promise.resolve();
        this._destroyed = true;

        const editor = this.editor;
        this.editor = null;

        this._destroyPromise = (async () => {
            if (editor) {
                if (!fromEditor && editor.isFullscreen) {
                    const exitCallback = editor.onFullscreenExit;
                    editor.onFullscreenExit = null;
                    if (typeof editor._requestExitFullscreen === "function") {
                        await editor._requestExitFullscreen({ reason: "controller_destroy" });
                    } else {
                        editor._exitFullscreen();
                    }
                    editor.onFullscreenExit = exitCallback;
                }
                editor.destroy();
            }

            this.controller.onFullscreenSessionDestroyed(this);
        })();
        return this._destroyPromise;
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
            font-family: ${FONT.sans};
            font-size: 11px;
            overflow: hidden;
        `);
        this.root.dataset.sonderEditor = "1";

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
            color: ${CHROME.text};
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        `);
        projectTitle.textContent = summary?.name || state.projectName || "Sonder Editor";
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
        if (state.isFullscreenOpen) badges.push({ text: "Editor Active", state: "running" });
        if (state.activeOwner?.host_mode === "tab") {
            const orphaned = state.activeOwner?.status === "orphaned";
            badges.push({
                text: orphaned ? "Mounted Tab Stale" : "Mounted Tab",
                state: orphaned ? "pending" : "running",
            });
        }
        if (state.activeOwner && state.activeOwner.host_mode !== "tab" && state.activeOwner.session_id !== this.controller._editorSessionId) {
            badges.push({ text: "Owned", state: "pending" });
        }
        if ((queueCounts.running || 0) > 0) badges.push({ text: `${queueCounts.running} Running`, state: "running" });
        if ((queueCounts.pending || 0) > 0) badges.push({ text: `${queueCounts.pending} Pending`, state: "pending" });
        if (!badges.length && summary) badges.push({ text: "Idle", state: "idle" });
        for (const badge of badges) {
            this._badgeRowEl.appendChild(makeStatusPill(badge.text, badge.state));
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
            formatCountLabel("R", assetCounts.artifact),
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
        const owner = state.activeOwner || null;
        const ownedByThis = owner?.session_id === this.controller._editorSessionId;
        const ownedByTab = owner?.host_mode === "tab";
        const ownedExternally = !!owner && !ownedByThis && !ownedByTab;
        const pendingHandoff = !!state.pendingTabHandoff;
        const actionLabel = pendingHandoff
            ? "Waiting for Tab"
            : state.isFullscreenOpen
                ? "Editor Active"
                : ownedByTab
                    ? "Open Mounted Editor"
                    : ownedExternally
                        ? "Owned by Workflow"
                        : "Open Editor";
        const actionDisabled = !state.projectDir || state.isFullscreenOpen || pendingHandoff || ownedExternally;
        const openBtn = style(document.createElement("button"), `
            ${buttonStyle((state.isFullscreenOpen || ownedByTab || ownedExternally || pendingHandoff) ? "subtle" : "primary")}
            cursor: ${state.projectDir && !actionDisabled ? "pointer" : "default"};
        `);
        openBtn.textContent = actionLabel;
        openBtn.disabled = actionDisabled;
        openBtn.addEventListener("click", () => {
            if (ownedByTab) this.controller.focusMountedTab();
            else this.controller.openFullscreen();
        });
        this._actionRowEl.appendChild(openBtn);
        if (owner && !ownedByThis) {
            const ownerLine = style(document.createElement("div"), `
                color: ${CHROME.textDim};
                font-size: 10px;
                display: flex;
                align-items: center;
                gap: 8px;
            `);
            const text = document.createElement("span");
            const ownerStatus = owner?.status === "orphaned" ? " (stale)" : "";
            text.textContent = ownedByTab
                ? `Mounted tab${ownerStatus}: ${owner.workflow_label || "Persistent editor tab"}`
                : `Owner${ownerStatus}: ${owner.workflow_label || owner.host_mode || "editor"}`;
            ownerLine.appendChild(text);
            const release = style(document.createElement("button"), `
                ${buttonStyle("ghost")}
                padding: 2px 6px;
                font-size: 10px;
            `);
            release.textContent = "Force Release";
            release.addEventListener("click", () => this.controller.forceReleaseOwner(owner));
            ownerLine.appendChild(release);
            this._actionRowEl.appendChild(ownerLine);
        }

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
        this._moduleContainerEl.dataset.sonderModuleSizing = this.getModuleHostSizing(moduleId);
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

    _getModuleDef(moduleId) {
        return moduleId ? (this.controller.modules?.[moduleId] || null) : null;
    }

    getModuleHostSizing(moduleId) {
        const moduleDef = this._getModuleDef(moduleId);
        return moduleDef?.hostSizing || "auto";
    }

    isFillModule(moduleId) {
        return this.getModuleHostSizing(moduleId) === "fill";
    }

    shouldAutoResizeNode(moduleId) {
        if (!moduleId) return false;
        const moduleDef = this._getModuleDef(moduleId);
        const nodeResize = moduleDef?.nodeResize || (this.isFillModule(moduleId) ? "manual" : "auto");
        return nodeResize === "auto";
    }

    _measureAvailableModuleHeight() {
        const rootHeight = this.root.clientHeight || this.root.offsetHeight || 0;
        if (!rootHeight) return 0;

        const rootRect = this.root.getBoundingClientRect();
        const containerRect = this._moduleContainerEl.getBoundingClientRect();
        const layoutWidth = this.root.offsetWidth || this.root.clientWidth || 0;
        const layoutHeight = this.root.offsetHeight || this.root.clientHeight || 0;
        let visualScale = 1;
        if (layoutWidth > 0 && rootRect.width > 0) {
            visualScale = rootRect.width / layoutWidth;
        } else if (layoutHeight > 0 && rootRect.height > 0) {
            visualScale = rootRect.height / layoutHeight;
        }
        if (!Number.isFinite(visualScale) || visualScale <= 0) {
            visualScale = 1;
        }
        const rootStyle = window.getComputedStyle(this.root);
        const paddingBottom = parseFloat(rootStyle.paddingBottom) || 0;
        const containerTop = Math.max(
            0,
            ((containerRect.top - rootRect.top) / visualScale) - (this.root.clientTop || 0)
        );
        return Math.max(0, Math.floor(rootHeight - paddingBottom - containerTop));
    }

    syncModuleContainerHeight() {
        const moduleId = this.controller.state.expandedModuleId;
        if (!moduleId || this._moduleContainerEl.style.display === "none") return;
        this._applyModuleContainerSizing(moduleId);
        if (!this.isFillModule(moduleId)) {
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
        const shouldAutoResizeNode = this.shouldAutoResizeNode(moduleId);

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
        installProjectVersionFetchPatch();
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
            maskPreOffset: this._getWidgetValue("mask_pre_offset", 0),
            maskPostOffset: this._getWidgetValue("mask_post_offset", 0),
            renderQueueActive: coerceBoolean(this._getWidgetValue("render_queue_active", true), true),
            dormantSummary: null,
            moduleCache: this.moduleCache,
            isFullscreenOpen: false,
            activeOwner: null,
            sessionStatus: "",
            lastOwnerSignature: "",
            canvasHostConnected: false,
            syncConnectionState: "closed",
            pendingTabHandoff: null,
            expandedModuleId: "",
            _projectReadyQueue: [],
        };
        this.moduleStatus = {
            assets: { loading: false, error: "" },
            preview: { loading: false, error: "" },
            queue: { loading: false, error: "" },
        };
        this._moduleLoadAborters = {};
        this._summaryAborter = null;
        this._previewStateRefreshTimer = null;
        this._pendingPreviewRefreshKeys = new Set();
        this._pendingPreviewRefreshSyncAssets = false;
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
        this._frameConstraintHealedFor = "";
        this._editorSessionId = newEditorSessionId("fullscreen");
        this._sessionHeartbeatTimer = null;
        this._suppressNextSessionRelease = false;
        this._syncConnection = null;
        this._syncProjectId = "";
        this._syncHostId = "";
        this._syncSourceNodeId = "";
        this._syncRetryTimer = null;
        this._syncSubscribed = false;
        this._widgetStatePollTimer = null;
        this._widgetStatePollInFlight = false;
        this._canvasHostHeartbeatTimer = null;
        this._ownerPollTimer = null;
        this._ownerPollIntervalMs = 0;
        this._ownerPollInFlight = false;
        this._ownerPollQueued = false;
        this._pendingHandoffTimer = null;
        this._canvasInstanceId = getCanvasInstanceId();
        this._cachedHostNodeId = "";
        this._cachedHostId = "";
        this._mountedTabWindowName = `sonder-editor-tab-${this._editorSessionId}`;
        this._diagEvents = [];
        this._diagBoot = null;
        this._diagHotkeyUnregister = null;
        this._diagClearHook = () => this._clearDiagEvents();
        // Register hotkey unconditionally; handler checks flag at press time.
        this._registerDiagHotkey();
        this._registerDiagClearHook();
        this._diagBootIfNeeded();
    }

    _registerDiagClearHook() {
        // Self-register into the shared window.SonderClearDiag() registry so the
        // console command can reset this controller's diag ring without a reload.
        if (typeof window === "undefined") return;
        if (!(window.__SONDER_DIAG_CLEARERS instanceof Set)) {
            window.__SONDER_DIAG_CLEARERS = new Set();
        }
        window.__SONDER_DIAG_CLEARERS.add(this._diagClearHook);
    }

    _clearDiagEvents() {
        this._diagEvents.length = 0;
        this._diagBoot = null;
        this._diagBootIfNeeded();
    }

    _diagBootIfNeeded() {
        if (!isSessionDiagEnabled() || this._diagBoot) return;
        this._diagBoot = {
            kind: "boot",
            t_wall: Date.now(),
            t_mono: performance.now(),
            build_marker: `canvas:${this._editorSessionId || ""}`,
            canvas_instance_id: this._canvasInstanceId,
        };
        this._diagEvents.push({ ...this._diagBoot });
    }

    _recordDiagEvent(kind, payload = {}) {
        if (!isSessionDiagEnabled()) return;
        this._diagBootIfNeeded();
        if (!this._diagHotkeyUnregister) this._registerDiagHotkey();
        if (this._diagEvents.length >= SESSION_DIAG_RING_MAX) {
            this._diagEvents.shift();
        }
        this._diagEvents.push({
            t_wall: Date.now(),
            t_mono: performance.now(),
            kind,
            ...payload,
        });
    }

    _logRender(reason = "") {
        if (!isSessionDiagEnabled()) return;
        let caller = reason;
        if (!caller) {
            const stack = (new Error()).stack || "";
            const lines = stack.split("\n");
            caller = (lines[2] || "").trim();
        }
        this._recordDiagEvent("render", { caller });
    }

    _registerDiagHotkey() {
        // Register unconditionally — flag check happens inside the handler so the
        // hotkey works even if the user flips `window.SONDER_DEBUG_SESSION` on
        // after the controller was constructed.
        if (this._diagHotkeyUnregister) {
            try { this._diagHotkeyUnregister(); } catch (_) {}
            this._diagHotkeyUnregister = null;
        }
        try {
            this._diagHotkeyUnregister = registerKeyboardConsumer({
                id: `session-diag-dump:${this._editorSessionId}`,
                priority: KEYBOARD_PRIORITY?.EDITOR ?? 10,
                keydown: (event) => {
                    if (!isSessionDiagEnabled()) return false;
                    if (!event.ctrlKey || !event.altKey || !event.shiftKey) return false;
                    if (String(event.key || "").toLowerCase() !== "d") return false;
                    this._dumpSessionDiagnostics().catch(() => {});
                    return true;
                },
            });
        } catch (_) {
            this._diagHotkeyUnregister = null;
        }
    }

    async _dumpSessionDiagnostics() {
        const projectDir = this.state?.projectDir || "";
        let backendDiag = null;
        if (projectDir) {
            try {
                const response = await fetch(`/sonder-editor/session/${encodeURIComponent(projectDir)}/diag`);
                if (response.ok) backendDiag = await response.json();
            } catch (_) { backendDiag = null; }
        }
        const canvasDiag = (typeof window !== "undefined" && window.__SONDER_CANVAS_DIAG) || null;
        const bundle = {
            captured_at_wall: Date.now(),
            captured_at_mono: performance.now(),
            actor: "canvas_controller",
            session_id: this._editorSessionId,
            project_dir: projectDir,
            controller: {
                boot: this._diagBoot ? { ...this._diagBoot } : null,
                events: this._diagEvents.slice(),
            },
            canvas_page: canvasDiag ? {
                boot: canvasDiag.boot ? { ...canvasDiag.boot } : null,
                events: Array.isArray(canvasDiag.events) ? canvasDiag.events.slice() : [],
            } : null,
            backend: backendDiag,
        };
        const json = JSON.stringify(bundle, null, 2);
        try {
            await navigator.clipboard.writeText(json);
            console.info("[Sonder Session Diag] Copied diagnostic bundle to clipboard:", bundle);
        } catch (_) {
            try {
                const blob = new Blob([json], { type: "application/json" });
                const url = URL.createObjectURL(blob);
                const anchor = document.createElement("a");
                anchor.href = url;
                anchor.download = `sonder-session-diag-canvas-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
                document.body.appendChild(anchor);
                anchor.click();
                document.body.removeChild(anchor);
                setTimeout(() => URL.revokeObjectURL(url), 5000);
                console.info("[Sonder Session Diag] Downloaded diagnostic bundle:", bundle);
            } catch (err) {
                console.warn("[Sonder Session Diag] Failed to copy/download diagnostic bundle:", err);
            }
        }
    }

    destroy() {
        if (this._destroyed) return;
        this._destroyed = true;

        if (this._diagHotkeyUnregister) {
            try { this._diagHotkeyUnregister(); } catch (_) {}
            this._diagHotkeyUnregister = null;
        }
        if (typeof window !== "undefined" && window.__SONDER_DIAG_CLEARERS instanceof Set && this._diagClearHook) {
            window.__SONDER_DIAG_CLEARERS.delete(this._diagClearHook);
        }
        this._diagEvents.length = 0;

        if (this._summaryAborter) {
            this._summaryAborter.abort();
            this._summaryAborter = null;
        }

        clearTimeout(this._previewStateRefreshTimer);
        this._previewStateRefreshTimer = null;
        this._pendingPreviewRefreshKeys.clear();
        this._pendingPreviewRefreshSyncAssets = false;

        for (const moduleId of Object.keys(this._moduleLoadAborters)) {
            this._abortModuleLoad(moduleId);
        }

        clearTimeout(this._pendingHandoffTimer);
        this._pendingHandoffTimer = null;
        this._teardownProjectSync();
        this.card.teardown();

        if (this.fullscreenSession) {
            const session = this.fullscreenSession;
            this.fullscreenSession = null;
            session.destroy();
        } else {
            this._releaseFullscreenSession();
        }

        this.state._projectReadyQueue.length = 0;
    }

    getElement() {
        return this.root;
    }

    getHeight() {
        return this._height;
    }

    render() {
        if (this._destroyed) return;
        this._logRender();
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

    // Align _height with an already-restored node.size[1] (e.g. workflow load).
    // Overhead is recomputed here because widget visibility (Create button, hidden
    // workflow widgets) can change it between calls.
    adoptLoadedNodeHeight() {
        if (this._destroyed) return;
        const nodeHeight = Math.max(0, this.node.size?.[1] || 0);
        if (nodeHeight <= 0) return;
        const computed = this.node.computeSize?.();
        const totalComputed = computed?.[1] || nodeHeight;
        const overhead = Math.max(0, totalComputed - 150);
        this._height = Math.max(150, nodeHeight - overhead);
        this.card.syncModuleContainerHeight?.();
    }

    // Wraps node.setSize in _programmaticResize so handleNodeResize treats it
    // as programmatic. Per durable_rules.md > Dormant Resize Contract.
    setNodeSizeProgrammatic(width, height) {
        if (this._destroyed) return;
        const w = Math.max(0, Number(width) || 0);
        const h = Math.max(0, Number(height) || 0);
        this._programmaticResize = true;
        try {
            this.node.setSize?.([w, h]);
        } finally {
            this._programmaticResize = false;
        }
    }

    queueResize() {
        if (this._destroyed || this._resizeScheduled) return;
        this._resizeScheduled = true;
        requestAnimationFrame(() => {
            this._resizeScheduled = false;
            if (this._destroyed) return;
            if (this.state.isFullscreenOpen) return;
            if (this.root.style.display === "none") return;
            const expandedModuleId = this.state.expandedModuleId;
            // queueResize only drives node height when an auto-resize module is mounted.
            // No-module and manual-module states leave node height to the user — required
            // for workflow-loaded sizes to survive collapse/expand cycles.
            if (!expandedModuleId || !this.card.shouldAutoResizeNode?.(expandedModuleId)) {
                this.card.syncModuleContainerHeight?.();
                return;
            }
            const currentWidth = Math.max(240, this.node.size?.[0] || 0);
            const currentHeight = Math.max(0, this.node.size?.[1] || 0);
            this.card.syncModuleContainerHeight?.();
            const measured = Math.ceil(this.root.scrollHeight || this.root.offsetHeight || this.root.clientHeight || 190);
            this._height = Math.max(150, measured + 10);
            if (Math.abs(currentHeight - this._height) > 1) {
                this.setNodeSizeProgrammatic(currentWidth, this._height);
            }
        });
    }

    _buildModules() {
        // `hostSizing` controls the module container; `nodeResize` controls whether the node grows to content.
        return {
            assets: {
                id: "assets",
                title: "Assets",
                hostSizing: "fill",
                nodeResize: "manual",
                resourceTier: "light",
                load: async (controller, signal) => await controller._loadDormantAssets(signal),
                mount: (container, data, controller) => controller._mountAssetsModule(container, data),
                collapseCleanup: () => {},
                invalidate: (keys) => keys.some(key => key === "project" || key === "assets" || key === "scene"),
            },
            preview: {
                id: "preview",
                title: "Preview",
                hostSizing: "fill",
                nodeResize: "auto",
                resourceTier: "media",
                load: async (controller, signal) => await controller._loadPreviewModule(signal),
                mount: (container, data, controller) => controller._mountPreviewModule(container, data),
                collapseCleanup: () => {},
                invalidate: (keys) => keys.some(key => key === "project" || key === "scene" || key === "assets" || key === "queue" || key === "preview"),
            },
            queue: {
                id: "queue",
                title: "Queue",
                hostSizing: "fill",
                nodeResize: "manual",
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
        this.state.maskPreOffset = Math.max(0, parseInt(this._getWidgetValue("mask_pre_offset", 0), 10) || 0);
        this.state.maskPostOffset = Math.max(0, parseInt(this._getWidgetValue("mask_post_offset", 0), 10) || 0);
        this.state.renderQueueActive = coerceBoolean(this._getWidgetValue("render_queue_active", true), true);
    }

    onEditorWidgetValueChange(name, value, { publish = true, refreshPreview = true } = {}) {
        if (name === "scene_id") this.state.sceneId = value || "";
        if (name === "selection_start") this.state.selectionStart = Math.max(0, parseInt(value, 10) || 0);
        if (name === "selection_end") this.state.selectionEnd = Math.max(0, parseInt(value, 10) || 0);
        if (name === "pre_context_frames") this.state.preContextFrames = Math.max(0, parseInt(value, 10) || 0);
        if (name === "post_context_frames") this.state.postContextFrames = Math.max(0, parseInt(value, 10) || 0);
        if (name === "mask_pre_offset") this.state.maskPreOffset = Math.max(0, parseInt(value, 10) || 0);
        if (name === "mask_post_offset") this.state.maskPostOffset = Math.max(0, parseInt(value, 10) || 0);
        if (name === "render_queue_active") this.state.renderQueueActive = coerceBoolean(value, true);
        if (publish) this._seedWidgetState({ [name]: value }, { seed: false });
        const previewKeys = this._previewInvalidationKeysForWidget(name);
        if (refreshPreview && previewKeys.length) {
            this._schedulePreviewStateRefresh(previewKeys);
        }
        this.render();
    }

    _previewInvalidationKeysForWidget(name) {
        if (!PREVIEW_WIDGET_FIELDS.has(name)) return [];
        if (name === "scene_id") return ["scene", "preview"];
        if (name === "render_queue_active") return ["queue", "preview"];
        return ["preview"];
    }

    _schedulePreviewStateRefresh(keys = ["preview"], { syncAssets = false } = {}) {
        if (this._destroyed || !this.state.projectDir) return;
        for (const key of keys || []) {
            this._pendingPreviewRefreshKeys.add(key);
        }
        this._pendingPreviewRefreshSyncAssets = this._pendingPreviewRefreshSyncAssets || !!syncAssets;
        clearTimeout(this._previewStateRefreshTimer);
        this._previewStateRefreshTimer = setTimeout(() => {
            this._previewStateRefreshTimer = null;
            const pendingKeys = Array.from(this._pendingPreviewRefreshKeys);
            const pendingSyncAssets = this._pendingPreviewRefreshSyncAssets;
            this._pendingPreviewRefreshKeys.clear();
            this._pendingPreviewRefreshSyncAssets = false;
            this._refreshSummaryThenReloadModules(pendingKeys, { syncAssets: pendingSyncAssets });
        }, PREVIEW_STATE_REFRESH_DEBOUNCE_MS);
    }

    _sourceNodeId() {
        return String(this.node?.id ?? "");
    }

    _hostId() {
        const sourceNodeId = this._sourceNodeId() || "anon";
        if (!this._cachedHostId || this._cachedHostNodeId !== sourceNodeId) {
            this._cachedHostNodeId = sourceNodeId;
            this._cachedHostId = `${this._canvasInstanceId}:${sourceNodeId}`;
        }
        return this._cachedHostId;
    }

    _currentWidgetStateValues() {
        const values = {};
        for (const name of EDITOR_WIDGET_FIELDS) {
            const fallback = name === "scene_id" ? ""
                : name === "take_placement_mode" ? "trimmed"
                    : name === "take_placement_linked" ? true
                        : name === "take_placement_muted" ? false
                            : name === "render_queue_active" ? true
                                : 0;
            values[name] = this._getWidgetValue(name, fallback);
        }
        return values;
    }

    _teardownProjectSync({ preserveRetry = false } = {}) {
        if (!preserveRetry) {
            clearTimeout(this._syncRetryTimer);
            this._syncRetryTimer = null;
        }
        clearInterval(this._canvasHostHeartbeatTimer);
        this._canvasHostHeartbeatTimer = null;
        clearInterval(this._widgetStatePollTimer);
        this._widgetStatePollTimer = null;
        this._widgetStatePollInFlight = false;
        clearTimeout(this._projectUpdatedDebounceTimer);
        this._projectUpdatedDebounceTimer = null;
        this._pendingProjectUpdatedEvent = null;
        this._syncSubscribed = false;
        this._stopOwnerPolling();
        if (this._syncConnection) {
            this._syncConnection.close();
            this._syncConnection = null;
        }
        this._syncProjectId = "";
        this._syncHostId = "";
        this._syncSourceNodeId = "";
        this.state.syncConnectionState = "closed";
        this.state.canvasHostConnected = false;
    }

    _projectSyncMatches() {
        const projectId = this._projectId();
        const sourceNodeId = this._sourceNodeId();
        if (!this._syncConnection || !projectId || !sourceNodeId) return false;
        return this._syncProjectId === projectId
            && this._syncHostId === this._hostId()
            && this._syncSourceNodeId === sourceNodeId;
    }

    _startCanvasHostHeartbeat(projectId, hostId, sourceNodeId) {
        clearInterval(this._canvasHostHeartbeatTimer);
        let lastSentAt = 0;
        const sendHeartbeat = () => {
            if (this._destroyed || projectId !== this._projectId() || hostId !== this._hostId() || sourceNodeId !== this._sourceNodeId()) return;
            const now = performance.now();
            const gapMs = lastSentAt > 0 ? now - lastSentAt : 0;
            lastSentAt = now;
            this._recordDiagEvent("canvas_host_heartbeat_send", { gap_ms: gapMs, project_id: projectId });
            heartbeatCanvasHost(projectId, this._editorSessionId, hostId, sourceNodeId, this._fullscreenOwnerInfo()).then((result) => {
                this._recordDiagEvent("canvas_host_heartbeat_recv", {
                    duration_ms: performance.now() - now,
                    ok: !!(result && result.ok),
                    canvas_host_connected: !!(result && result.canvas_host_connected),
                    code: result?.code || "",
                });
                if (this._destroyed || projectId !== this._projectId() || hostId !== this._hostId()) return;
                if (Object.prototype.hasOwnProperty.call(result, "canvas_host_connected")) {
                    const connected = !!result.canvas_host_connected;
                    if (this.state.canvasHostConnected !== connected) {
                        this.state.canvasHostConnected = connected;
                        this.render();
                    }
                }
            }).catch((error) => {
                this._recordDiagEvent("canvas_host_heartbeat_error", {
                    duration_ms: performance.now() - now,
                    error: String(error && error.message ? error.message : error),
                });
                console.warn("[Sonder] Canvas host heartbeat failed:", error);
            });
        };
        sendHeartbeat();
        this._canvasHostHeartbeatTimer = setInterval(sendHeartbeat, 5000);
    }

    _setupProjectSync() {
        if (this._destroyed) return;
        const projectId = this._projectId();
        if (!projectId) {
            this._teardownProjectSync();
            return;
        }
        clearTimeout(this._syncRetryTimer);
        this._syncRetryTimer = null;
        const sourceNodeId = this._sourceNodeId();
        if (!sourceNodeId) {
            this._teardownProjectSync({ preserveRetry: true });
            this.state.syncConnectionState = "waiting_for_node_id";
            this.state.canvasHostConnected = false;
            this.render();
            this._syncRetryTimer = setTimeout(() => {
                if (this._destroyed) return;
                this._syncRetryTimer = null;
                this._setupProjectSync();
            }, 250);
            return;
        }
        const hostId = this._hostId();
        this._teardownProjectSync();
        this._syncProjectId = projectId;
        this._syncHostId = hostId;
        this._syncSourceNodeId = sourceNodeId;
        this._syncSubscribed = false;
        this._startCanvasHostHeartbeat(projectId, hostId, sourceNodeId);
        this._syncConnection = connectProjectSync(projectId, {
            onProjectUpdated: (event) => this._handleProjectUpdatedFromSync(event),
            onWidgetStateChanged: (event) => this._handleWidgetStateChanged(event),
            onSessionChanged: (event) => this._handleSessionChanged(event),
            onHostPresenceChanged: (event) => this._handleHostPresenceChanged(event),
            onSubscribed: (event) => this._handleSyncSubscribed(event),
            onConnectionState: (state, details = {}) => {
                if (state !== "open") this._syncSubscribed = false;
                this._recordDiagEvent("sync_transport_state", { state, url: details.url || "" });
                if (this.state.syncConnectionState !== state) {
                    this.state.syncConnectionState = state;
                    this.render();
                }
            },
        }, {
            role: "canvas_host",
            clientId: this._canvasInstanceId,
            hostId,
            sourceNodeId,
            sessionId: this._editorSessionId,
            workflowLabel: document.title || "ComfyUI workflow",
        });
        this._seedWidgetState(this._currentWidgetStateValues(), { seed: true });
        this._startWidgetStateFallbackPolling(projectId, hostId, sourceNodeId);
        this._startOwnerPolling(4000);
        getEditorSession(projectId, hostId, sourceNodeId).then((result) => {
            if (this._destroyed || projectId !== this._projectId()) return;
            this._applyOwnerState(result.owner || null, "initial");
        }).catch(() => {});
    }

    _seedWidgetState(values = this._currentWidgetStateValues(), { seed = false } = {}) {
        const projectId = this._projectId();
        if (!projectId) return;
        putEditorWidgetState(projectId, this._sourceNodeId(), this._editorSessionId, values, { seed, hostId: this._hostId() }).catch((error) => {
            console.warn("[Sonder] Failed to publish editor widget state:", error);
        });
    }

    _applyRemoteWidgetState(values = {}, source = "unknown", remoteSessionId = "") {
        if (!values || typeof values !== "object") return false;
        const changedFields = [];
        const previewRefreshKeys = new Set();
        for (const [name, value] of Object.entries(values)) {
            if (!EDITOR_WIDGET_FIELDS.includes(name)) continue;
            if (Object.is(this._getWidgetValue(name), value)) continue;
            this._setWidgetValue(name, value);
            this.onEditorWidgetValueChange(name, value, { publish: false, refreshPreview: false });
            for (const key of this._previewInvalidationKeysForWidget(name)) {
                previewRefreshKeys.add(key);
            }
            changedFields.push(name);
        }
        if (!changedFields.length) return false;
        this._recordDiagEvent("widget_state_apply", {
            source,
            remote_session_id: remoteSessionId || "",
            fields: changedFields,
        });
        this.node?.setDirtyCanvas?.(true, true);
        this.fullscreenSession?.editor?.applyWidgetState?.(values);
        if (previewRefreshKeys.size) {
            this._schedulePreviewStateRefresh(Array.from(previewRefreshKeys));
        } else {
            this.refreshSummary().finally(() => this.render());
        }
        return true;
    }

    _startWidgetStateFallbackPolling(projectId, hostId, sourceNodeId) {
        clearInterval(this._widgetStatePollTimer);
        this._widgetStatePollTimer = setInterval(() => {
            this._pollWidgetStateFallback(projectId, hostId, sourceNodeId);
        }, WIDGET_STATE_FALLBACK_POLL_MS);
        this._pollWidgetStateFallback(projectId, hostId, sourceNodeId);
    }

    _pollWidgetStateFallback(projectId, hostId, sourceNodeId) {
        if (this._destroyed || !projectId || !hostId || !sourceNodeId) return;
        if (projectId !== this._projectId() || hostId !== this._hostId() || sourceNodeId !== this._sourceNodeId()) return;
        if (this._syncSubscribed && this.state.syncConnectionState === "open") return;
        if (this._widgetStatePollInFlight) return;
        this._widgetStatePollInFlight = true;
        const sentAt = performance.now();
        this._recordDiagEvent("widget_state_poll_send", {
            sync_state: this.state.syncConnectionState,
            sync_subscribed: this._syncSubscribed,
        });
        getEditorWidgetState(projectId, sourceNodeId, hostId).then((payload) => {
            this._recordDiagEvent("widget_state_poll_recv", {
                duration_ms: performance.now() - sentAt,
                ok: !!payload?.ok,
                canvas_host_connected: !!payload?.canvas_host_connected,
                field_count: Object.keys(payload?.state || payload?.values || {}).length,
            });
            if (this._destroyed || projectId !== this._projectId() || hostId !== this._hostId() || sourceNodeId !== this._sourceNodeId()) return;
            if (Object.prototype.hasOwnProperty.call(payload || {}, "canvas_host_connected")) {
                const connected = !!payload.canvas_host_connected;
                if (this.state.canvasHostConnected !== connected) {
                    this.state.canvasHostConnected = connected;
                    this.render();
                }
            }
            const values = payload?.state && typeof payload.state === "object"
                ? payload.state
                : (payload?.values && typeof payload.values === "object" ? payload.values : {});
            this._applyRemoteWidgetState(values, "poll", payload?.session_id || "");
        }).catch((error) => {
            this._recordDiagEvent("widget_state_poll_error", {
                duration_ms: performance.now() - sentAt,
                error: String(error && error.message ? error.message : error),
            });
        }).finally(() => {
            this._widgetStatePollInFlight = false;
        });
    }

    _handleProjectUpdatedFromSync(event) {
        if (!event || event.project_id !== this._projectId()) return;
        // Debounced self-echo guard (mutation-integrity F4): the WS broadcast of
        // our own save always arrives BEFORE the mutation response that records
        // the new version in the client map, so an immediate version compare
        // misfires every time. Stash the latest event and re-check after 250 ms
        // (timer re-armed per event, fires once) — by then own-save responses
        // have landed and the guard filters correctly; event bursts coalesce to
        // one fanout; genuinely-remote updates (take arrival, queue completion)
        // refresh with an imperceptible +250 ms. 250 ms is a conscious
        // hard-code (transport tuning, not a user preference).
        this._pendingProjectUpdatedEvent = event;
        clearTimeout(this._projectUpdatedDebounceTimer);
        this._projectUpdatedDebounceTimer = setTimeout(() => {
            this._projectUpdatedDebounceTimer = null;
            const pending = this._pendingProjectUpdatedEvent;
            this._pendingProjectUpdatedEvent = null;
            if (!pending || pending.project_id !== this._projectId()) return;
            const currentVersion = getProjectVersion(pending.project_id);
            const shouldRefresh = !!(pending.modified_at && pending.modified_at !== currentVersion);
            this._recordDiagEvent("project_updated_recv", {
                event_modified_at: pending.modified_at || "",
                current_version: currentVersion || "",
                refresh: shouldRefresh,
            });
            if (!shouldRefresh) return;
            this._refreshSummaryThenReloadModules(["project", "assets", "scene", "queue", "preview"], { syncAssets: true });
            this.fullscreenSession?.refresh(["project", "assets", "scenes", "queue"]);
        }, 250);
    }

    _handleWidgetStateChanged(event) {
        if (!event || event.project_id !== this._projectId()) return;
        if (String(event.host_id || "") !== this._hostId()) return;
        if (String(event.source_node_id || "") !== this._sourceNodeId()) return;
        if (String(event.session_id || "") === this._editorSessionId) return;
        const values = event.values && typeof event.values === "object" ? event.values : {};
        this._recordDiagEvent("ws_widget_state_changed", {
            remote_session_id: event.session_id || "",
            field_count: Object.keys(values).length,
            fields: Object.keys(values).sort(),
        });
        this._applyRemoteWidgetState(values, "ws", event.session_id || "");
    }

    _ownerSignature(owner) {
        if (!owner) return "";
        return JSON.stringify({
            host_mode: owner.host_mode || "",
            session_id: owner.session_id || "",
            status: owner.status || "active",
            source_node_id: owner.source_node_id || "",
            workflow_label: owner.workflow_label || "",
            browser_instance_id: owner.browser_instance_id || "",
        });
    }

    _reconcileOwnerStateEffects(owner, source = "unknown") {
        let changed = false;

        if (this.state.pendingTabHandoff && owner?.host_mode === "tab") {
            clearTimeout(this._pendingHandoffTimer);
            this._pendingHandoffTimer = null;
            this.state.pendingTabHandoff = null;
            this._startOwnerPolling(4000);
            this._recordDiagEvent("pending_tab_handoff_cleared", { source });
            changed = true;
        }

        if (owner?.host_mode === "tab" && owner.session_id !== this._editorSessionId && this.state.isFullscreenOpen && this.fullscreenSession) {
            const session = this.fullscreenSession;
            this._suppressNextSessionRelease = true;
            this.fullscreenSession = null;
            this._recordDiagEvent("tab_owner_destroy_fullscreen", {
                source,
                owner_session_id: owner.session_id || "",
            });
            session.destroy();
            changed = true;
        }

        return changed;
    }

    _applyOwnerState(owner, source = "unknown") {
        const normalized = owner || null;
        const signature = this._ownerSignature(normalized);
        const previousSignature = this.state.lastOwnerSignature;
        if (signature === previousSignature) {
            this._recordDiagEvent("apply_owner_noop", {
                source,
                signature_unchanged: true,
                status: normalized?.status || "",
                host_mode: normalized?.host_mode || "",
            });
            if (this._reconcileOwnerStateEffects(normalized, source)) {
                this.render();
            }
            return false;
        }
        this._recordDiagEvent("apply_owner_change", {
            source,
            previous_signature: previousSignature,
            next_signature: signature,
            status: normalized?.status || "",
            host_mode: normalized?.host_mode || "",
            session_id: normalized?.session_id || "",
            same_session: normalized?.session_id === this._editorSessionId,
        });
        this.state.lastOwnerSignature = signature;
        this.state.activeOwner = normalized;
        this.state.sessionStatus = normalized?.status || "";

        this._reconcileOwnerStateEffects(normalized, source);

        this.render();
        return true;
    }

    _stopOwnerPolling() {
        clearInterval(this._ownerPollTimer);
        this._ownerPollTimer = null;
        this._ownerPollIntervalMs = 0;
        this._ownerPollInFlight = false;
        this._ownerPollQueued = false;
    }

    _startOwnerPolling(intervalMs = 4000) {
        const projectId = this._projectId();
        const hostId = this._hostId();
        const sourceNodeId = this._sourceNodeId();
        if (!projectId || !hostId || !sourceNodeId) return;
        if (this._ownerPollTimer && this._ownerPollIntervalMs === intervalMs) return;
        clearInterval(this._ownerPollTimer);
        this._ownerPollIntervalMs = intervalMs;
        const poll = () => this._pollOwnerState(intervalMs <= 500 ? "handoff_poll" : "poll");
        poll();
        this._ownerPollTimer = setInterval(poll, intervalMs);
    }

    _pollOwnerState(source = "poll") {
        const projectId = this._projectId();
        const hostId = this._hostId();
        const sourceNodeId = this._sourceNodeId();
        if (this._destroyed || !projectId || !hostId || !sourceNodeId) return;
        if (this._ownerPollInFlight) {
            this._ownerPollQueued = true;
            this._recordDiagEvent("owner_poll_queue", { source });
            return;
        }
        this._ownerPollInFlight = true;
        const sentAt = performance.now();
        this._recordDiagEvent("owner_poll_send", { source, interval_ms: this._ownerPollIntervalMs });
        getEditorSession(projectId, hostId, sourceNodeId).then((result) => {
            this._recordDiagEvent("owner_poll_recv", {
                source,
                duration_ms: performance.now() - sentAt,
                has_owner: !!(result && result.owner),
                owner_status: result?.owner?.status || "",
                owner_host_mode: result?.owner?.host_mode || "",
            });
            if (this._destroyed || projectId !== this._projectId() || hostId !== this._hostId() || sourceNodeId !== this._sourceNodeId()) return;
            this._applyOwnerState(result.owner || null, source);
        }).catch((err) => {
            this._recordDiagEvent("owner_poll_error", {
                source,
                duration_ms: performance.now() - sentAt,
                error: String(err && err.message ? err.message : err),
            });
        }).finally(() => {
            this._ownerPollInFlight = false;
            if (this._ownerPollQueued && !this._destroyed) {
                this._ownerPollQueued = false;
                this._pollOwnerState("queued");
            }
        });
    }

    _handleSessionChanged(event) {
        if (!event || event.project_id !== this._projectId()) return;
        if (String(event.host_id || "") !== this._hostId()) return;
        this._recordDiagEvent("ws_session_changed", {
            owner_status: event?.owner?.status || "",
            owner_host_mode: event?.owner?.host_mode || "",
            owner_session_id: event?.owner?.session_id || "",
        });
        this._pollOwnerState("ws_notify");
    }

    _handleHostPresenceChanged(event) {
        if (!event || event.project_id !== this._projectId()) return;
        if (String(event.host_id || "") !== this._hostId()) return;
        if (String(event.source_node_id || "") !== this._sourceNodeId()) return;
        const connected = !!event.canvas_host_connected;
        this._recordDiagEvent("ws_host_presence", { connected, previous_connected: this.state.canvasHostConnected });
        if (this.state.canvasHostConnected !== connected) {
            this.state.canvasHostConnected = connected;
            this.render();
        }
    }

    _handleSyncSubscribed(event) {
        if (!event || event.project_id !== this._projectId()) return;
        if (event.host_id && String(event.host_id) !== this._hostId()) return;
        if (event.source_node_id && String(event.source_node_id) !== this._sourceNodeId()) return;
        this._syncSubscribed = true;
        this._recordDiagEvent("ws_sync_subscribed", {
            canvas_host_connected: !!event?.canvas_host_connected,
            previous_connected: this.state.canvasHostConnected,
            has_canvas_host_field: Object.prototype.hasOwnProperty.call(event, "canvas_host_connected"),
        });
        if (Object.prototype.hasOwnProperty.call(event, "canvas_host_connected")) {
            const connected = !!event.canvas_host_connected;
            if (this.state.canvasHostConnected !== connected) {
                this.state.canvasHostConnected = connected;
                this.render();
            }
        }
    }

    whenProjectReady(callback) {
        if (typeof callback !== "function") return;
        if (this.state.projectDir) {
            try { callback(); }
            catch (error) { console.warn("[Sonder] whenProjectReady callback error:", error); }
            return;
        }
        this.state._projectReadyQueue.push(callback);
    }

    _drainProjectReadyQueue() {
        if (!this.state.projectDir) return;
        const queue = this.state._projectReadyQueue.splice(0);
        for (const callback of queue) {
            try { callback(); }
            catch (error) { console.warn("[Sonder] whenProjectReady drain error:", error); }
        }
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
            this._frameConstraintHealedFor = "";
            this.state.projectDir = "";
            this.state.dormantSummary = null;
            this.state.activeOwner = null;
            this.state.sessionStatus = "";
            this.state.lastOwnerSignature = "";
            this.state.pendingTabHandoff = null;
            this.state._projectReadyQueue.length = 0;
            this._teardownProjectSync();
            this._invalidateModules(["project", "assets", "scene", "queue", "preview"]);
            this.collapseModule();
            this.render();
            return;
        }

        const previousProjectDir = this.state.projectDir || "";
        if (projectDir !== previousProjectDir) {
            const switchingExistingProject = !!previousProjectDir && previousProjectDir !== projectDir;
            if (this.fullscreenSession) {
                const session = this.fullscreenSession;
                this.fullscreenSession = null;
                session.destroy();
            }
            this._queueSaveCompletionCounter = 0;
            this._lastQueueSettledSaveCompletionCounter = 0;
            this._frameConstraintHealedFor = "";
            this.state.projectDir = projectDir;
            this.state.dormantSummary = null;
            if (switchingExistingProject) {
                this.state.sceneId = "";
                this.state.selectionStart = 0;
                this.state.selectionEnd = 0;
                this._setWidgetValue("scene_id", "");
                this._setWidgetValue("selection_start", 0);
                this._setWidgetValue("selection_end", 0);
            }
            this._invalidateModules(["project", "assets", "scene", "queue", "preview"]);
            this.state.expandedModuleId = "";
            this.state.activeOwner = null;
            this.state.sessionStatus = "";
            this.state.lastOwnerSignature = "";
            this.state.pendingTabHandoff = null;
            this._setupProjectSync();
        } else if (!this._projectSyncMatches()) {
            this._setupProjectSync();
        }

        if (this._frameConstraintHealedFor !== projectDir) {
            try {
                await healProjectFrameConstraint(projectDir);
                this._frameConstraintHealedFor = projectDir;
            } catch (error) {
                console.warn("[Sonder] Failed to heal project frame constraint:", error);
            }
        }

        await this.refreshSummary();
        this._seedWidgetState(this._currentWidgetStateValues(), { seed: true });
        this.render();
        this._drainProjectReadyQueue();
    }

    async refreshSummary(options = {}) {
        const { syncAssets = false } = options;
        if (this._destroyed || !this.state.projectDir) return false;

        if (this._summaryAborter) {
            this._summaryAborter.abort();
        }
        const aborter = new AbortController();
        this._summaryAborter = aborter;
        let refreshed = false;

        try {
            if (syncAssets) {
                const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets`), {
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
            refreshed = true;
            const activeScene = this.state.dormantSummary?.active_scene;
            const widgetValue = this._getWidgetValue("scene_id", "") || "";
            if (activeScene?.scene_id && !this.state.sceneId && !widgetValue) {
                this.state.sceneId = activeScene.scene_id;
                this._setWidgetValue("scene_id", activeScene.scene_id);
            }
        } catch (e) {
            if (e.name !== "AbortError") {
                console.warn("[Sonder] Failed to fetch dormant summary:", e);
            }
        } finally {
            if (this._summaryAborter === aborter) {
                this._summaryAborter = null;
            }
            this.render();
        }
        return refreshed;
    }

    async _refreshSummaryThenReloadModules(keys = [], options = {}) {
        const refreshed = await this.refreshSummary(options);
        if (this._destroyed || !refreshed) return false;
        this._invalidateModules(keys);
        this._reloadExpandedModuleIfNeeded(keys);
        this.render();
        return true;
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
                this._abortModuleLoad(moduleId);
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

    _projectId() {
        return projectIdFromDir(this.state.projectDir);
    }

    focusMountedTab() {
        const projectId = this._projectId();
        if (!projectId) return;
        const tabName = this.state.activeOwner?.browser_instance_id || this._mountedTabWindowName;
        const opened = window.open("", tabName);
        if (opened) {
            try {
                if (opened.location?.href === "about:blank") {
                    opened.close?.();
                    window.alert?.("The mounted editor tab could not be focused. Use Force Release if the tab was closed.");
                    return;
                }
                opened.focus?.();
            } catch (_err) {}
            return;
        }
        window.alert?.("The mounted editor tab could not be focused. Use Force Release if the tab was closed.");
    }

    async forceReleaseOwner(owner) {
        const projectId = this._projectId();
        const sessionId = owner?.session_id || "";
        if (!projectId || !sessionId) return;
        const ok = window.confirm?.("Force release the mounted editor session? Unsaved tab-only widget edits may be lost.") ?? true;
        if (!ok) return;
        const result = await releaseEditorSession(projectId, sessionId, true, this._hostId(), this._sourceNodeId());
        if (!result.ok) {
            window.alert?.(result.code ? `Could not release editor session (${result.code}).` : "Could not release editor session.");
            return;
        }
        this.state.pendingTabHandoff = null;
        this._applyOwnerState(null, "force_release");
    }

    _startSessionHeartbeat() {
        clearInterval(this._sessionHeartbeatTimer);
        const projectId = this._projectId();
        if (!projectId) return;
        const sendHeartbeat = () => {
            const sessionId = this._editorSessionId;
            heartbeatEditorSession(projectId, sessionId, this._hostId(), this._sourceNodeId())
                .then((result) => {
                    // Self-stop on backend rejection. If the backend says
                    // we're no longer the owner (tab took over via handoff,
                    // force-release, orphan expiry, or session TTL), keep
                    // heartbeating only adds noise (logged 409s) and racks
                    // up rejected requests. The owner-poll path is the
                    // authoritative recovery mechanism — let it handle
                    // re-claiming if appropriate.
                    if (result && result.ok === false && this._sessionHeartbeatTimer) {
                        this._recordDiagEvent("session_heartbeat_self_stop", {
                            session_id: sessionId,
                            code: result.code || "",
                            owner_session_id: result.owner?.session_id || "",
                            owner_host_mode: result.owner?.host_mode || "",
                        });
                        clearInterval(this._sessionHeartbeatTimer);
                        this._sessionHeartbeatTimer = null;
                    }
                })
                .catch(() => {});
        };
        sendHeartbeat();
        this._sessionHeartbeatTimer = setInterval(sendHeartbeat, 10000);
    }

    _releaseFullscreenSession() {
        clearInterval(this._sessionHeartbeatTimer);
        this._sessionHeartbeatTimer = null;
        const projectId = this._projectId();
        if (!projectId || this._suppressNextSessionRelease) {
            this._suppressNextSessionRelease = false;
            return;
        }
        releaseEditorSession(projectId, this._editorSessionId, false, this._hostId(), this._sourceNodeId()).catch(() => {});
    }

    _fullscreenOwnerInfo() {
        return {
            host_id: this._hostId(),
            source_node_id: this._sourceNodeId(),
            browser_instance_id: this._editorSessionId,
            workflow_label: document.title || "ComfyUI workflow",
        };
    }

    async _ensureFullscreenSessionOwner() {
        const projectId = this._projectId();
        if (!projectId) throw new Error("No project selected");
        const current = await getEditorSession(projectId, this._hostId(), this._sourceNodeId());
        const owner = current.owner || null;
        if (owner?.session_id === this._editorSessionId) {
            this._applyOwnerState(owner, "ensure_fullscreen");
            this._startSessionHeartbeat();
            heartbeatEditorSession(projectId, this._editorSessionId, this._hostId(), this._sourceNodeId()).catch(() => {});
            return owner;
        }
        if (owner) {
            const label = owner?.host_mode ? `${owner.host_mode} editor` : "another editor";
            throw new Error(`Project is already owned by ${label}.`);
        }
        await this._claimFullscreenSession();
        return this.state.activeOwner;
    }

    async _claimFullscreenSession() {
        const projectId = this._projectId();
        if (!projectId) throw new Error("No project selected");
        installProjectVersionFetchPatch();
        const claim = await claimEditorSession(projectId, this._editorSessionId, "fullscreen", this._fullscreenOwnerInfo(), "", this._hostId());
        if (!claim.ok) {
            const owner = claim.owner;
            const label = owner?.host_mode ? `${owner.host_mode} editor` : "another editor";
            throw new Error(`Project is already owned by ${label}.`);
        }
        this._applyOwnerState(claim.owner || null, "claim_fullscreen");
        this._seedWidgetState(this._currentWidgetStateValues(), { seed: true });
        this._startSessionHeartbeat();
    }

    async mountFullscreenInTab() {
        const projectId = this._projectId();
        if (!projectId || !this.fullscreenSession) return;
        const opened = window.open("about:blank", this._mountedTabWindowName);
        if (!opened) {
            window.alert?.("The browser blocked the editor tab.");
            return;
        }
        try {
            await this._ensureFullscreenSessionOwner();
            let handoff = await createEditorHandoff(projectId, this._editorSessionId, this._hostId(), this._sourceNodeId());
            if (!handoff.ok && handoff.code === "not_owner" && !handoff.owner) {
                await this._claimFullscreenSession();
                handoff = await createEditorHandoff(projectId, this._editorSessionId, this._hostId(), this._sourceNodeId());
            }
            if (!handoff.ok || !handoff.token) {
                const owner = handoff.owner;
                const reason = handoff.code ? ` (${handoff.code})` : "";
                const ownerLabel = owner?.host_mode ? ` Current owner: ${owner.host_mode}.` : "";
                throw new Error(`Could not create editor handoff token${reason}.${ownerLabel}`);
            }
            this.state.pendingTabHandoff = { token: handoff.token, openedAt: Date.now() };
            this.render();
            clearTimeout(this._pendingHandoffTimer);
            this._pendingHandoffTimer = setTimeout(() => {
                if (!this.state.pendingTabHandoff) return;
                this.state.pendingTabHandoff = null;
                this._startOwnerPolling(4000);
                this.render();
                window.alert?.("The editor tab did not claim the session. Check popup blockers or try again.");
            }, 10000);
            const url = api.apiURL(
                `/sonder-editor/tab/${encodeURIComponent(projectId)}?handoff=${encodeURIComponent(handoff.token)}&host_id=${encodeURIComponent(this._hostId())}&source_node_id=${encodeURIComponent(this._sourceNodeId())}&session_name=${encodeURIComponent(this._mountedTabWindowName)}`
            );
            opened.location.href = url;
            this._startOwnerPolling(400);
        } catch (error) {
            opened.close?.();
            this.state.pendingTabHandoff = null;
            clearTimeout(this._pendingHandoffTimer);
            this._pendingHandoffTimer = null;
            this._startOwnerPolling(4000);
            this.render();
            console.warn("[Sonder] Failed to mount editor in tab:", error);
            window.alert?.(error?.message || String(error));
        }
    }

    async openFullscreen() {
        if (this._destroyed || !this.state.projectDir || this.fullscreenSession || this.state.isFullscreenOpen) return;
        const owner = this.state.activeOwner;
        if (owner?.host_mode === "tab") {
            this.focusMountedTab();
            return;
        }
        if (owner && owner.session_id !== this._editorSessionId) {
            window.alert?.("This project is currently owned by another editor session.");
            return;
        }

        this.syncStateFromWidgets();
        try {
            await this._claimFullscreenSession();
        } catch (error) {
            console.warn("[Sonder] Failed to claim fullscreen editor session:", error);
            window.alert?.(error?.message || String(error));
            return;
        }
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
            console.warn("[Sonder] Failed to open fullscreen editor:", e);
            if (this.fullscreenSession) {
                const failedSession = this.fullscreenSession;
                this.fullscreenSession = null;
                try {
                    failedSession.destroy();
                } catch (cleanupErr) {
                    console.warn("[Sonder] Failed to clean up fullscreen session after mount error:", cleanupErr);
                }
            }
            this.fullscreenSession = null;
            this.state.isFullscreenOpen = false;
            this._releaseFullscreenSession();
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
        this._releaseFullscreenSession();

        this.state.isFullscreenOpen = false;
        this.syncStateFromWidgets();
        const refreshKeys = ["scene", "assets", "queue", "preview"];
        const restoreModuleId = this._preFullscreenModuleId;
        this._preFullscreenModuleId = "";
        this.refreshSummary().then((refreshed) => {
            if (this._destroyed || !refreshed) return;
            this._invalidateModules(refreshKeys);
            if (restoreModuleId && this.modules[restoreModuleId]) {
                this.expandModule(restoreModuleId);
            } else {
                this._reloadExpandedModuleIfNeeded(refreshKeys);
            }
            this.render();
        });
    }

    handleNodeExecuted() {
        if (this._destroyed || !this.state.projectDir) return;
        this.syncStateFromWidgets();
        this._refreshSummaryThenReloadModules(["assets", "scene", "queue", "preview"], { syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
    }

    handleSaveVideoExecuted() {
        if (this._destroyed || !this.state.projectDir) return;
        this._queueSaveCompletionCounter += 1;
        this.syncStateFromWidgets();
        this._refreshSummaryThenReloadModules(["assets", "scene", "queue", "preview"], { syncAssets: true });
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
    }

    async handleBridgeExecutionSettled({ allowRollback = false, attemptIndex = 0 } = {}) {
        if (this._destroyed || !this.state.projectDir) {
            return this.state.dormantSummary?.queue_counts || {};
        }
        this.syncStateFromWidgets();
        // Bridge-asset-arrival info toast (observe-only — does NOT touch the
        // rollback ladder logic). Baseline the asset total at the first rung of
        // the settle session, then announce once when a new asset registers.
        if (attemptIndex === 0) {
            this._bridgeSettleBaselineTotal = this.state.dormantSummary?.asset_counts?.total ?? 0;
            this._bridgeArrivalAnnounced = false;
        }
        const refreshed = await this.refreshSummary({ syncAssets: true });
        if (refreshed) {
            this._invalidateModules(["assets", "scene", "queue", "preview"]);
            this._reloadExpandedModuleIfNeeded(["assets", "scene", "queue", "preview"]);
        }
        if (!this._bridgeArrivalAnnounced && typeof this._bridgeSettleBaselineTotal === "number") {
            const newTotal = this.state.dormantSummary?.asset_counts?.total ?? 0;
            const added = newTotal - this._bridgeSettleBaselineTotal;
            if (added > 0) {
                notifyInfo(added > 1 ? `${added} bridge assets saved` : "Bridge asset saved");
                this._bridgeArrivalAnnounced = true;
            }
        }
        const counts = this.state.dormantSummary?.queue_counts || {};
        this.fullscreenSession?.refresh(["assets", "scenes", "queue"]);
        this.render();
        return counts;
    }

    async handleQueueExecutionSettled({ allowRollback = false } = {}) {
        if (this._destroyed || !this.state.projectDir) {
            return this.state.dormantSummary?.queue_counts || {};
        }
        this.syncStateFromWidgets();
        let refreshed = await this.refreshSummary();
        if (refreshed) {
            this._invalidateModules(["queue", "preview"]);
            this._reloadExpandedModuleIfNeeded(["queue", "preview"]);
        }
        let counts = this.state.dormantSummary?.queue_counts || {};
        const sawSaveCompletion = this._queueSaveCompletionCounter > this._lastQueueSettledSaveCompletionCounter;
        if (allowRollback && (counts.running || 0) > 0 && !sawSaveCompletion) {
            const rolledBack = await this._rollbackStaleRunningQueueJobs();
            if (rolledBack) {
                refreshed = await this.refreshSummary();
                if (refreshed) {
                    this._invalidateModules(["queue", "preview"]);
                    this._reloadExpandedModuleIfNeeded(["queue", "preview"]);
                }
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

        const list = Array.from(files);
        const total = list.length;
        const handle = notifyProgress({
            verb: "Importing",
            message: total > 1 ? `0/${total} files` : (list[0]?.name || "1 file"),
            progress: total > 1 ? { current: 0, total, unit: "" } : null,
            foreground: true,
            source: "import",
        });

        let importedAny = false;
        let done = 0;
        let imported = 0;
        try {
            const failures = [];
            for (const file of list) {
                try {
                    if (await importFileIntoProject(this.state.projectDir, file, folder)) {
                        importedAny = true;
                        imported += 1;
                    }
                } catch (error) {
                    failures.push({ file, error });
                    console.warn("[Sonder] Import failed:", file?.name, error);
                }
                done += 1;
                handle.update({
                    message: total > 1 ? `${done}/${total} files` : (file?.name || ""),
                    progress: total > 1 ? { current: done, total, unit: "" } : null,
                });
            }

            if (importedAny) {
                this._invalidateModules(["assets", "scene"]);
                this._reloadExpandedModuleIfNeeded(["assets", "scene"]);
                await this.refreshSummary({ syncAssets: true });
            }
            if (!failures.length && imported === total) {
                handle.resolve({ message: `Imported ${imported} file${imported === 1 ? "" : "s"}` });
            } else if (imported > 0) {
                const first = failures[0]?.error?.message || "one file failed";
                handle.resolve({ tier: "warning", message: `Imported ${imported} of ${total} files. ${first}` });
            } else {
                const first = failures[0]?.error?.message || "No files imported.";
                handle.resolve({ tier: "error", message: first });
            }
        } catch (e) {
            console.error("[Sonder] Import failed:", e);
            handle.resolve({ tier: "error", message: e?.message || "Import failed." });
        }

        return importedAny;
    }

    async _updateAssetMetadata(assetId, updates) {
        if (!this.state.projectDir || !assetId) return null;

        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/${assetId}`), {
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
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/${assetId}/usages`));
        if (!resp.ok) {
            throw new Error(`Asset usage fetch failed: ${resp.status}`);
        }
        return await resp.json();
    }

    async _getBulkAssetUsages(assetIds) {
        if (!this.state.projectDir || !Array.isArray(assetIds) || !assetIds.length) return null;
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-usages`), {
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
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-move`), {
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

        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/${assetId}`), {
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

        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/restore`), {
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
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-restore`), {
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
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-delete`), {
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

        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/permanent`), {
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
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/bulk-permanent-delete`), {
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
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/empty-trash`), {
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
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/folders`), {
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
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/folders`), {
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
        const resp = await fetch(api.apiURL(`/sonder-editor/project/${projectIdFromDir(this.state.projectDir)}/assets/folders`), {
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
        const sceneId = this.state.sceneId || "";
        const assetsPromise = fetchJson(buildDormantAssetsUrl(this.state.projectDir), signal);
        const scenePromise = sceneId
            ? fetchJson(buildSceneUrl(this.state.projectDir, sceneId), signal).catch((error) => {
                if (error?.name === "AbortError") throw error;
                console.warn("[Sonder] Failed to load scene for asset gallery scope:", error);
                return null;
            })
            : Promise.resolve(null);
        const [payload, scene] = await Promise.all([assetsPromise, scenePromise]);
        const normalized = Array.isArray(payload)
            ? { assets: payload, folders: [] }
            : { assets: payload.assets || [], folders: payload.folders || [] };
        return {
            ...normalized,
            currentSceneAssetIds: deriveCurrentSceneAssetIds(scene, normalized.assets),
        };
    }

    async _loadQueueModule(signal) {
        const queue = await fetchJson(buildQueueUrl(this.state.projectDir), signal);
        return Array.isArray(queue) ? queue : [];
    }

    async _loadPreviewModule(signal) {
        const summary = this.state.dormantSummary;
        const activeQueueJob = summary?.active_queue_job || null;
        const queuePreviewActive = coerceBoolean(this.state.renderQueueActive, true) && !!activeQueueJob;
        const sceneId = (queuePreviewActive && activeQueueJob?.scene_id)
            ? activeQueueJob.scene_id
            : (summary?.active_scene?.scene_id || this.state.sceneId);
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
        const selection = summary?.active_scene?.selection || {};
        const resolveRange = (startValue, endValue) => {
            if (durationFrames <= 0) return [0, 0];
            const start = clamp(Math.max(0, parseInt(startValue, 10) || 0), 0, durationFrames);
            const end = clamp(Math.max(0, parseInt(endValue, 10) || 0), 0, durationFrames);
            if (end > start) return [start, end];
            return [0, durationFrames];
        };

        let rangeStartFrame = 0;
        let rangeEndFrame = durationFrames;
        let previewSource = "selection";
        let sourceLabel = summary?.active_scene?.name || scene?.name || "Scene";
        if (queuePreviewActive) {
            [rangeStartFrame, rangeEndFrame] = resolveRange(
                activeQueueJob.selection_start,
                activeQueueJob.selection_end
            );
            previewSource = "queue";
            sourceLabel = activeQueueJob.scene_name || scene?.name || sourceLabel;
        } else {
            [rangeStartFrame, rangeEndFrame] = resolveRange(
                selection.generation_start_frame,
                selection.generation_end_frame
            );
            if (selection.is_full_scene) {
                previewSource = "scene";
            }
        }
        const hasPreviewRange = durationFrames > 0 && rangeEndFrame > rangeStartFrame;
        const rawInitialFrame = (queuePreviewActive || previewSource === "scene")
            ? rangeStartFrame
            : (parseInt(this.state.selectionStart, 10) || rangeStartFrame);
        const initialFrame = hasPreviewRange
            ? clamp(
                rawInitialFrame,
                rangeStartFrame,
                Math.max(rangeStartFrame, rangeEndFrame - 1)
            )
            : 0;
        const queueFps = queuePreviewActive ? Number(activeQueueJob.scene_fps) : 0;
        const queueWidth = queuePreviewActive ? parseInt(activeQueueJob.scene_width, 10) : 0;
        const queueHeight = queuePreviewActive ? parseInt(activeQueueJob.scene_height, 10) : 0;
        // Resolved executing prompt, matching the range source: queue jobs
        // carry their frozen prompt verbatim; live falls back to the scene
        // payload's context-window compose
        const previewPrompt = queuePreviewActive
            ? String(activeQueueJob.preview_prompt ?? "")
            : String(summary?.active_scene?.preview_prompt ?? "");
        // Dormant preview is frame-accurate like fullscreen, so auto/blob use
        // whole-file blob loading. Direct streaming remains an explicit opt-in.
        const streamingModeSetting = getEditorSettings()?.playback?.streamingMode ?? "auto";
        const streamingMode = streamingModeSetting === "direct" ? "direct" : "blob";
        return {
            kind: "viewport",
            streamingMode,
            streamingModeSetting,
            label: "Viewport Preview",
            subtitle: previewSource === "queue" ? `${sourceLabel} - Queue` : sourceLabel,
            previewPrompt,
            scene,
            assets,
            fps: Math.max(
                1,
                queueFps
                    || Number(scene?.fps)
                    || Number(summary?.active_scene?.effective_fps)
                    || Number(summary?.fps)
                    || 24
            ),
            initialFrame,
            durationFrames,
            rangeStartFrame,
            rangeEndFrame,
            previewSource,
            activeQueueJob: queuePreviewActive ? activeQueueJob : null,
            frameWidth: Math.max(
                1,
                queueWidth
                    || parseInt(scene?.width, 10)
                    || parseInt(summary?.active_scene?.effective_width, 10)
                    || 768
            ),
            frameHeight: Math.max(
                1,
                queueHeight
                    || parseInt(scene?.height, 10)
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
            ownerId: `sonder-editor-${this.node?.id ?? "anon"}:dormant-gallery`,
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
            onOpenSourceWorkflow: async (asset) => {
                await window.__SONDER_OPEN_SOURCE_WORKFLOW__?.(this.state.projectDir, asset);
            },
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
            overflow: hidden;
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
        if (data.previewPrompt) {
            // Resolved executing prompt (frozen job prompt on queue source,
            // live context-window compose otherwise); 2-line clamp, full
            // text on hover
            const promptLine = style(document.createElement("div"), `
                color: ${CHROME.textDim};
                font-size: 10px;
                line-height: 1.35;
                word-break: break-word;
                display: -webkit-box;
                -webkit-line-clamp: 2;
                -webkit-box-orient: vertical;
                overflow: hidden;
            `);
            promptLine.textContent = data.previewPrompt;
            promptLine.title = data.previewPrompt;
            header.appendChild(promptLine);
        }
        wrap.appendChild(header);

        const surface = style(document.createElement("div"), `
            flex: 1 1 0;
            min-height: 0;
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
            accent-color: ${THEME.accent};
        `;
        const frameLabel = style(document.createElement("div"), `
            color: ${THEME.fg1};
            font-size: 10px;
            min-width: 92px;
            text-align: right;
            white-space: nowrap;
            font-family: ${FONT.mono};
            margin-left: auto;
        `);
        scrubRow.append(scrubber, frameLabel);
        transport.appendChild(scrubRow);

        const projectDir = this.state.projectDir;
        const mediaStreamingMode = data.streamingMode === "direct" ? "direct" : "blob";
        dormantBoundaryDebugEvent("resolved-mode", {
            mode: mediaStreamingMode,
            setting: data.streamingModeSetting || "",
        });
        const assetsByPath = new Map((data.assets || []).map((asset) => [asset.path, asset]));
        const effectiveFps = Math.max(1, Number(data.fps) || 24);
        const totalFrames = Math.max(0, parseInt(data.durationFrames, 10) || 0);
        const rawRangeStart = Math.max(0, parseInt(data.rangeStartFrame, 10) || 0);
        const rawRangeEnd = Math.max(0, parseInt(data.rangeEndFrame, 10) || totalFrames);
        let rangeStartFrame = clamp(rawRangeStart, 0, totalFrames);
        let rangeEndFrame = clamp(rawRangeEnd, 0, totalFrames);
        if (rangeEndFrame <= rangeStartFrame) {
            rangeStartFrame = 0;
            rangeEndFrame = totalFrames;
        }
        const hasFrameRange = totalFrames > 0 && rangeEndFrame > rangeStartFrame;
        const firstRenderableFrame = hasFrameRange ? rangeStartFrame : 0;
        const lastRenderableFrame = hasFrameRange ? Math.max(rangeStartFrame, rangeEndFrame - 1) : 0;
        const playableFrameCount = hasFrameRange ? rangeEndFrame - rangeStartFrame : 0;
        const overlapsPreviewRange = (startValue, endValue) => {
            if (!hasFrameRange) return false;
            const start = Math.max(0, parseInt(startValue, 10) || 0);
            const end = Math.max(0, parseInt(endValue, 10) || 0);
            return start < rangeEndFrame && end > rangeStartFrame;
        };
        const hasSceneAudio = (data.scene?.audio_tracks || []).some((track) => {
            if (track?.muted) return false;
            if (!overlapsPreviewRange(track.timeline_start_frame, track.timeline_end_frame)) return false;
            if (isAudioLaneHidden(data.scene, track.lane_index || 0)) return false;
            return !assetsByPath.get(track.source_path)?.missing;
        });

        let currentFrame = hasFrameRange
            ? clamp(parseInt(data.initialFrame, 10) || firstRenderableFrame, firstRenderableFrame, lastRenderableFrame)
            : 0;
        let destroyed = false;
        let isPlaying = false;
        let audioEnabled = hasSceneAudio;
        let playbackRaf = 0;
        let renderRaf = 0;
        let pendingRenderForceSeek = false;
        let playbackStartTs = 0;
        let playbackStartFrame = currentFrame;
        let currentTarget = null;
        let lastTargetSignature = "";
        let lastCommittedVideoSignature = "";
        let playbackBlockedSinceMs = 0;
        let lastPlaybackBlockKey = "";
        let lastPlaybackFallbackKey = "";
        let lastPlaybackTimeoutKey = "";
        let activeVideoKeys = new Set();
        let activeAudioKeys = new Set();
        const previewImageCache = new Map();
        const videoEntries = new Map();
        const audioEntries = new Map();
        const dormantSourceLoadCounts = new Map();
        const DORMANT_PLAYBACK_DRIFT_SEEK_SEC = 0.35;
        const DORMANT_PLAYBACK_DRIFT_SEEK_COOLDOWN_MS = 250;
        const DORMANT_PLAYBACK_BLOCK_HOLD_MS = 400;

        const clipPlaybackKey = (clip) => clip?.clip_id || `${clip?.source_path || ""}:${clip?.timeline_start_frame || 0}:${clip?.track_index || 0}`;
        const audioPlaybackKey = (track) => track?.track_id || `${track?.source_path || ""}:${track?.timeline_start_frame || 0}:${track?.lane_index || 0}`;
        const renderFrameIndex = () => (hasFrameRange ? clamp(currentFrame, firstRenderableFrame, lastRenderableFrame) : 0);
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

        function targetSignature(target) {
            if (!target) return "none";
            if (target.kind === "video") {
                return `video:${target.key || clipPlaybackKey(target.clip)}:${target.clip?.source_path || ""}`;
            }
            if (target.kind === "composite") {
                const layerKey = (target.layers || [])
                    .map((layer) => `${layer?.key || clipPlaybackKey(layer?.clip)}:${layer?.sourcePath || layer?.clip?.source_path || ""}`)
                    .join(",");
                return `composite:${layerKey}`;
            }
            if (target.kind === "image") {
                return `image:${target.posterUrl || ""}`;
            }
            if (target.kind === "missing") {
                return `missing:${target.subtitle || target.label || ""}`;
            }
            return target.kind || "empty";
        }

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
                .filter(track => overlapsPreviewRange(track.timeline_start_frame, track.timeline_end_frame))
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
            return (sourceFrame + 0.5) / effectiveFps;
        }

        function sourceTimeForAudio(track, frame) {
            const sourceFrame = Math.max(
                0,
                frame - (parseInt(track?.timeline_start_frame, 10) || 0) + (parseInt(track?.source_in_frame, 10) || 0)
            );
            return sourceFrame / effectiveFps;
        }

        function dormantNowMs() {
            return (typeof performance !== "undefined" && typeof performance.now === "function")
                ? performance.now()
                : Date.now();
        }

        function mediaHasMetadata(mediaEl) {
            return (mediaEl?.readyState || 0) >= 1 || Number.isFinite(Number(mediaEl?.duration));
        }

        function mediaIsDrawable(mediaEl) {
            return !!mediaEl && !mediaEl.seeking && (mediaEl.readyState || 0) >= 2;
        }

        function mediaTimeMatches(mediaEl, desiredTime, tolerance = 0.08) {
            return Math.abs((mediaEl?.currentTime || 0) - desiredTime) <= tolerance;
        }

        function expectedDormantPlaybackTime(entry, frame) {
            const startedAtFrame = Number.isFinite(entry?.startedAtFrame) ? entry.startedAtFrame : frame;
            const startedAtMediaTime = Number.isFinite(entry?.startedAtMediaTime) ? entry.startedAtMediaTime : 0;
            return startedAtMediaTime + ((frame - startedAtFrame) / effectiveFps);
        }

        function logDormantVideoState(entry, stateName, details = {}) {
            if (!entry) return;
            const debugKey = `${stateName}:${details.reason || ""}:${details.targetTime ?? ""}:${details.readyState ?? ""}`;
            if (entry.lastDebugState === debugKey) return;
            entry.lastDebugState = debugKey;
            dormantBoundaryDebugEvent(stateName, {
                key: entry.key,
                sourcePath: entry.layer?.sourcePath || entry.layer?.clip?.source_path || "",
                ...details,
            });
        }

        function requestDormantVideoSeek(entry) {
            const pending = entry?.pendingSeekTarget;
            const video = entry?.el;
            if (!pending || !video || !mediaHasMetadata(video)) return false;
            if (video.seeking && pending.requested) return false;
            const now = dormantNowMs();
            if (pending.requested && now - (pending.requestedAtMs || 0) < 150) return false;
            const desiredTime = clampMediaTime(video, pending.time);
            if (pending.requested && mediaTimeMatches(video, desiredTime, 0.04)) return false;
            try {
                video.currentTime = desiredTime;
                pending.time = desiredTime;
                pending.requested = true;
                pending.requestedAtMs = now;
                entry.requestedSeekTime = desiredTime;
                logDormantVideoState(entry, "video-seek-request", {
                    frame: pending.frame,
                    reason: pending.reason || "",
                    targetTime: Number(desiredTime.toFixed(4)),
                    readyState: video.readyState || 0,
                });
                return true;
            } catch (error) {
                return false;
            }
        }

        function settleDormantVideo(entry) {
            const pending = entry?.pendingSeekTarget;
            const video = entry?.el;
            if (!pending || !video || !mediaIsDrawable(video)) return false;
            const desiredTime = clampMediaTime(video, pending.time);
            if (!mediaTimeMatches(video, desiredTime, 0.08)) return false;
            entry.readyForDraw = true;
            entry.startedAtFrame = pending.frame;
            entry.startedAtMediaTime = video.currentTime || desiredTime;
            entry.lastCommittedFrame = null;
            entry.lastDrawnMediaTime = null;
            entry.pendingSeekTarget = null;
            entry.requestedSeekTime = Number.NaN;
            entry.lastDebugState = "";
            dormantBoundaryDebugEvent("video-seek-settled", {
                key: entry.key,
                sourcePath: entry.layer?.sourcePath || entry.layer?.clip?.source_path || "",
                frame: pending.frame,
                reason: pending.reason || "",
                mediaTime: Number((video.currentTime || desiredTime).toFixed(4)),
                readyState: video.readyState || 0,
            });
            return true;
        }

        function prepareDormantVideo(entry, layer, frame, reason, { force = false } = {}) {
            const video = entry?.el;
            if (!entry || !video || !layer?.clip) return false;
            entry.layer = layer;

            if (!force && entry.pendingSeekTarget) {
                requestDormantVideoSeek(entry);
                settleDormantVideo(entry);
                return entry.readyForDraw;
            }

            const targetTime = clampMediaTime(video, sourceTimeForClip(layer.clip, frame));
            if (
                !force
                && entry.readyForDraw
                && mediaIsDrawable(video)
                && mediaTimeMatches(video, targetTime, isPlaying ? 0.25 : 0.04)
            ) {
                return true;
            }

            entry.pendingTime = targetTime;
            entry.pendingSeekTarget = {
                time: targetTime,
                frame,
                reason,
                requested: false,
                requestedAtMs: 0,
            };
            entry.readyForDraw = false;
            entry.startedAtFrame = null;
            entry.startedAtMediaTime = 0;
            entry.lastCommittedFrame = null;
            entry.lastDrawnMediaTime = null;
            requestDormantVideoSeek(entry);
            settleDormantVideo(entry);
            return entry.readyForDraw;
        }

        function syncDormantVideoPlayback(entry, layer, frame, { forceSeek = false } = {}) {
            const video = entry?.el;
            if (!entry || !video) return false;
            entry.layer = layer;

            if (forceSeek || entry.pendingSeekTarget || !entry.readyForDraw) {
                prepareDormantVideo(entry, layer, frame, forceSeek ? "force" : "activate", { force: forceSeek });
            }

            if (!entry.readyForDraw) return false;

            if (mediaIsDrawable(video)) {
                const expectedTime = clampMediaTime(video, expectedDormantPlaybackTime(entry, frame));
                const drift = (video.currentTime || 0) - expectedTime;
                const now = dormantNowMs();
                if (
                    Math.abs(drift) > DORMANT_PLAYBACK_DRIFT_SEEK_SEC
                    && now - (entry.lastDriftSeekAtMs || 0) >= DORMANT_PLAYBACK_DRIFT_SEEK_COOLDOWN_MS
                ) {
                    entry.lastDriftSeekAtMs = now;
                    dormantBoundaryDebugEvent("video-drift-correct", {
                        key: entry.key,
                        sourcePath: entry.layer?.sourcePath || entry.layer?.clip?.source_path || "",
                        frame,
                        drift: Number(drift.toFixed(4)),
                        targetTime: Number(expectedTime.toFixed(4)),
                        currentTime: Number((video.currentTime || 0).toFixed(4)),
                    });
                    return prepareDormantVideo(entry, layer, frame, "drift", { force: true });
                }
            }

            video.muted = true;
            if (video.paused) {
                video.play().catch(() => {});
            }
            return true;
        }

        function isDormantVideoDrawable(entry, layer, frame, { playing = isPlaying } = {}) {
            const video = entry?.el;
            if (!entry || !video || !layer?.clip) return false;
            if (entry.pendingSeekTarget) {
                requestDormantVideoSeek(entry);
                settleDormantVideo(entry);
            }
            if (!mediaIsDrawable(video)) return false;
            if (playing) {
                return !!entry.readyForDraw;
            }
            const targetTime = clampMediaTime(video, sourceTimeForClip(layer.clip, frame));
            return mediaTimeMatches(video, targetTime, 0.04);
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
            video.preload = mediaStreamingMode === "direct" ? "metadata" : "auto";
            video.muted = true;
            video.playsInline = true;

            const entry = {
                key,
                layer,
                el: video,
                pendingTime: 0,
                startedAtFrame: null,
                startedAtMediaTime: 0,
                lastCommittedFrame: null,
                pendingSeekTarget: null,
                readyForDraw: false,
                lastDrawnMediaTime: null,
                lastDriftSeekAtMs: 0,
                cleanupHandle: null,
                listeners: [],
                cleanup: null,
                lastDebugState: "",
                readyLogged: false,
                requestedSeekTime: Number.NaN,
            };

            const onReady = () => {
                if (destroyed) return;
                if ((video.readyState || 0) >= 2 && !entry.readyLogged) {
                    dormantBoundaryDebugEvent("video-ready", {
                        key: entry.key,
                        sourcePath: entry.layer?.sourcePath || entry.layer?.clip?.source_path || "",
                        readyState: video.readyState || 0,
                    });
                    entry.readyLogged = true;
                }
                if (entry.pendingSeekTarget) {
                    requestDormantVideoSeek(entry);
                    settleDormantVideo(entry);
                }
                if (isPlaying && activeVideoKeys.has(entry.key) && entry.readyForDraw) {
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
                video,
                { mode: mediaStreamingMode }
            );
            const sourceLoadKey = `${key}:${layer.sourcePath}`;
            const sourceLoadCount = (dormantSourceLoadCounts.get(sourceLoadKey) || 0) + 1;
            dormantSourceLoadCounts.set(sourceLoadKey, sourceLoadCount);
            dormantBoundaryDebugEvent("video-source-created", {
                key,
                sourcePath: layer.sourcePath,
                count: sourceLoadCount,
                mode: mediaStreamingMode,
            });
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

            // Audio stays blob-loaded regardless of streamingMode (small files,
            // avoids network under-buffer/drift) — matches the fullscreen surface.
            entry.cleanupHandle = loadMediaAsBlob(
                buildProjectAssetViewURL(projectDir, track.source_path),
                audio,
                { mode: "blob" }
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
            return !!guideImage && drawDormantCanvasMedia(canvas, guideImage, dormantFitOptions(target.guide));
        }

        function updateTransport() {
            const frame = renderFrameIndex();
            scrubber.min = String(firstRenderableFrame);
            scrubber.max = String(lastRenderableFrame);
            scrubber.value = String(frame);
            frameLabel.textContent = hasFrameRange ? `f${frame} / ${lastRenderableFrame}` : "f0 / 0";
            playBtn.textContent = isPlaying ? "Pause" : "Play";
            playBtn.disabled = playableFrameCount <= 0;
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
            // Canvas is sized to the SCENE's aspect ratio (data.frameWidth/Height),
            // fitted inside the stage's content box (surface layout size - 16 for stage padding).
            // Surface's panelMuted background shows in the unused band, the asset
            // within the canvas is letterboxed at its native aspect by drawDormantCanvasMedia.
            const availableWidth = Math.max(1, Math.floor((surface.clientWidth || surface.offsetWidth || 0) - 16));
            const availableHeight = Math.max(1, Math.floor((surface.clientHeight || surface.offsetHeight || 0) - 16));
            const aspect = (Number(data.frameWidth) > 0 && Number(data.frameHeight) > 0)
                ? data.frameWidth / data.frameHeight
                : 1;
            let cssWidth = availableWidth;
            let cssHeight = Math.max(1, Math.floor(cssWidth / aspect));
            if (cssHeight > availableHeight) {
                cssHeight = availableHeight;
                cssWidth = Math.max(1, Math.floor(cssHeight * aspect));
            }
            if (canvas.width !== cssWidth) canvas.width = cssWidth;
            if (canvas.height !== cssHeight) canvas.height = cssHeight;
            canvas.style.width = `${cssWidth}px`;
            canvas.style.height = `${cssHeight}px`;
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
                if (image && drawDormantCanvasMedia(canvas, image, dormantFitOptions(target))) {
                    return;
                }
                drawDormantCanvasMessage(canvas, "Loading guide...", target.subtitle || "");
                return;
            }
            drawDormantCanvasMessage(canvas, target.label || "Preview unavailable", target.subtitle || "");
        }

        function syncVideoPlayback(target, frame, { forceSeek = false } = {}) {
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
                syncDormantVideoPlayback(entry, layer, frame, {
                    forceSeek: forceSeek || !activeVideoKeys.has(entry.key),
                });
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
            const frame = renderFrameIndex();
            const layers = getTargetLayers(target);

            if (!layers.length) {
                clearDormantCanvas(canvas);
                const drewGuide = drawGuideBackground(target);
                if (!drewGuide) {
                    drawDormantCanvasMessage(canvas, target.label || "No preview", target.subtitle || "");
                }
                lastCommittedVideoSignature = "";
                lastPlaybackBlockKey = "";
                lastPlaybackFallbackKey = "";
                lastPlaybackTimeoutKey = "";
                return;
            }

            if (!isPlaying) {
                clearDormantCanvas(canvas);
                let drewAny = drawGuideBackground(target);
                for (const layer of layers) {
                    const entry = ensureVideoEntry(layer);
                    if (!entry) continue;
                    entry.el.pause();
                    if (!isDormantVideoDrawable(entry, layer, frame, { playing: false })) {
                        prepareDormantVideo(entry, layer, frame, "scrub", { force: forceSeek });
                    }
                    if (isDormantVideoDrawable(entry, layer, frame, { playing: false })) {
                        drewAny = drawDormantCanvasMedia(canvas, entry.el, { opacity: layer.opacity ?? 1, ...dormantFitOptions(layer.clip) }) || drewAny;
                        entry.lastCommittedFrame = frame;
                        entry.lastDrawnMediaTime = entry.el.currentTime || 0;
                    }
                }
                if (!drewAny) {
                    drawDormantCanvasMessage(canvas, "Seeking frame...", target.subtitle || "");
                }
                lastCommittedVideoSignature = drewAny ? targetSignature(target) : "";
                lastPlaybackBlockKey = "";
                lastPlaybackFallbackKey = "";
                lastPlaybackTimeoutKey = "";
                return;
            }

            const drawableLayers = [];
            for (const layer of layers) {
                const entry = ensureVideoEntry(layer);
                if (!entry) continue;
                syncDormantVideoPlayback(entry, layer, frame, { forceSeek: false });
                if (isDormantVideoDrawable(entry, layer, frame, { playing: true })) {
                    drawableLayers.push({ entry, layer });
                }
            }

            if (drawableLayers.length > 0) {
                clearDormantCanvas(canvas);
                let drewAny = drawGuideBackground(target);
                for (const { entry, layer } of drawableLayers) {
                    drewAny = drawDormantCanvasMedia(canvas, entry.el, { opacity: layer.opacity ?? 1, ...dormantFitOptions(layer.clip) }) || drewAny;
                    entry.lastCommittedFrame = frame;
                    entry.lastDrawnMediaTime = entry.el.currentTime || 0;
                }
                lastCommittedVideoSignature = drewAny ? targetSignature(target) : "";
                playbackBlockedSinceMs = 0;
                lastPlaybackBlockKey = "";
                lastPlaybackFallbackKey = "";
                lastPlaybackTimeoutKey = "";
                return;
            }

            const blockKey = `${targetSignature(target)}:${layers.map(layer => layer.key || clipPlaybackKey(layer.clip)).join(",")}`;
            const now = dormantNowMs();
            if (lastPlaybackBlockKey !== blockKey) {
                playbackBlockedSinceMs = now;
                lastPlaybackBlockKey = blockKey;
                dormantBoundaryDebugEvent("video-blocked", {
                    frame,
                    targetKind: target?.kind || "",
                    layerCount: layers.length,
                });
            }
            const blockAgeMs = now - playbackBlockedSinceMs;
            if (lastCommittedVideoSignature && blockAgeMs < DORMANT_PLAYBACK_BLOCK_HOLD_MS) {
                return;
            }
            if (blockAgeMs >= DORMANT_PLAYBACK_BLOCK_HOLD_MS && lastPlaybackTimeoutKey !== blockKey) {
                lastPlaybackTimeoutKey = blockKey;
                dormantBoundaryDebugEvent("video-blocked-timeout", {
                    frame,
                    targetKind: target?.kind || "",
                    layerCount: layers.length,
                    blockedMs: Math.round(blockAgeMs),
                });
            }
            const fallbackKey = `${blockKey}:${canvas.width}x${canvas.height}`;
            if (lastPlaybackFallbackKey === fallbackKey) {
                return;
            }
            lastPlaybackFallbackKey = fallbackKey;
            clearDormantCanvas(canvas);
            const drewGuide = drawGuideBackground(target);
            if (!drewGuide) {
                drawDormantCanvasMessage(canvas, "Loading video...", target.subtitle || "");
            }
        }

        function renderPreviewFrame({ forceSeek = false } = {}) {
            if (destroyed) return;
            resizeCanvas();
            if (canvas.width < 4 || canvas.height < 4) {
                updateTransport();
                return;
            }
            const frame = renderFrameIndex();
            currentTarget = getTargetForFrame(frame);
            const nextTargetSignature = targetSignature(currentTarget);
            const targetChanged = nextTargetSignature !== lastTargetSignature;
            if (targetChanged) {
                dormantBoundaryDebugEvent("target-transition", {
                    frame,
                    from: lastTargetSignature,
                    to: nextTargetSignature,
                    kind: currentTarget?.kind || "none",
                });
                lastPlaybackBlockKey = "";
                lastPlaybackFallbackKey = "";
                lastPlaybackTimeoutKey = "";
                lastTargetSignature = nextTargetSignature;
            }
            const shouldForceSeek = forceSeek || targetChanged;

            if (isPlaying) {
                if (currentTarget?.kind === "video" || currentTarget?.kind === "composite") {
                    syncVideoPlayback(currentTarget, frame, { forceSeek: shouldForceSeek });
                } else {
                    pauseAllVideos();
                    lastCommittedVideoSignature = "";
                    lastPlaybackBlockKey = "";
                    lastPlaybackFallbackKey = "";
                    lastPlaybackTimeoutKey = "";
                }
                syncAudioPlayback(frame);
            } else {
                pauseAllVideos();
                pauseAllAudios();
            }

            if (currentTarget?.kind === "video" || currentTarget?.kind === "composite") {
                renderVideoTarget(currentTarget, { forceSeek: shouldForceSeek });
            } else {
                lastCommittedVideoSignature = "";
                lastPlaybackBlockKey = "";
                lastPlaybackFallbackKey = "";
                lastPlaybackTimeoutKey = "";
                renderStaticTarget(currentTarget || { kind: "empty", label: "No preview", subtitle: "" });
            }
            updateTransport();
        }

        const resizeObserver = new ResizeObserver(() => scheduleRender());
        resizeObserver.observe(surface);

        function stopPlayback({ preservePlayhead = true, shouldRender = true } = {}) {
            if (!preservePlayhead) {
                currentFrame = firstRenderableFrame;
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
            if (destroyed || playableFrameCount <= 0) return;
            if (currentFrame < firstRenderableFrame || currentFrame > lastRenderableFrame) {
                currentFrame = firstRenderableFrame;
            }
            isPlaying = true;
            playbackStartTs = performance.now();
            playbackStartFrame = renderFrameIndex();
            currentFrame = playbackStartFrame;
            const tick = (timestamp) => {
                if (!isPlaying || destroyed) return;
                const elapsedFrames = Math.floor(((timestamp - playbackStartTs) / 1000) * effectiveFps);
                const nextFrame = playbackStartFrame + elapsedFrames;
                currentFrame = clamp(nextFrame, firstRenderableFrame, lastRenderableFrame);
                renderPreviewFrame();
                if (nextFrame >= rangeEndFrame) {
                    currentFrame = lastRenderableFrame;
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
            currentFrame = hasFrameRange
                ? clamp(parseInt(scrubber.value, 10) || firstRenderableFrame, firstRenderableFrame, lastRenderableFrame)
                : 0;
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
        const settings = getEditorSettings();
        const timecodeMode = settings?.timelineBehavior?.timecodeMode === "timecode" ? "timecode" : "frames";
        const projectKey = projectIdFromDir(this.state.projectDir);
        const collapsedBatchIds = readQueueBatchCollapseState(projectKey, settings);
        const fallbackFps = Math.max(
            1,
            Number(this.state?.dormantSummary?.active_scene?.effective_fps)
                || Number(this.state?.dormantSummary?.fps)
                || 24
        );
        const handle = mountSharedRenderQueue(container, {
            jobs: Array.isArray(data) ? data : [],
            queueActive: this.state.renderQueueActive !== false,
            surface: "dormant",
            projectKey,
            timecodeMode,
            fallbackFps,
            collapsedBatchIds,
            emptyText: "Render queue is empty.",
            showDeleteJob: false,
            showClearCompleted: true,
            consumePointer: consumeDormantPointer,
            onSetQueueActive: (nextActive) => {
                this._setWidgetValue("render_queue_active", nextActive);
                this.state.renderQueueActive = nextActive;
                this._seedWidgetState({ render_queue_active: nextActive }, { seed: false });
                this._schedulePreviewStateRefresh(["queue", "preview"]);
                this.fullscreenSession?.editor?._setRenderQueueActive?.(nextActive, { syncWidget: false });
            },
            onClearCompleted: async () => {
                const response = await fetch(buildQueueUrl(this.state.projectDir), { method: "DELETE" });
                if (!response.ok) {
                    throw new Error(`Clear completed renders failed: ${response.status}`);
                }
                await this.refreshSummary();
                this._invalidateModules(["queue", "preview"]);
                this._reloadExpandedModuleIfNeeded(["queue", "preview"]);
                this.fullscreenSession?.refresh(["queue"]);
            },
            onBatchCollapsedChange: (nextCollapsedIds) => {
                persistQueueBatchCollapseState(projectKey, nextCollapsedIds, updateEditorSettings);
            },
        });

        return () => handle.destroy();
    }
}
