const { api } = window.comfyAPI.api;

import {
    DEFAULT_EDITOR_SETTINGS,
    GALLERY_SORT_OPTIONS,
    getEditorSettings,
    migrateLegacyGalleryProjectPrefs,
    subscribeEditorSettings,
    updateEditorSettings,
} from "./editor_settings.js";
import { register as registerKeyboardConsumer, PRIORITY as KEY_PRIORITY } from "./keyboard_ownership.js";

const DEFAULT_SORT_MODE = DEFAULT_EDITOR_SETTINGS.gallery.sortMode;
const DEFAULT_INSPECTOR_SETTINGS = DEFAULT_EDITOR_SETTINGS.inspector;
const ROOT_FOLDER_COLLAPSE_KEY = "__sonder_root__";
const TRASH_FOLDER_COLLAPSE_KEY = "__sonder_trash__";
const SORT_OPTIONS = GALLERY_SORT_OPTIONS;
const COMPARE_SORT_OPTIONS = GALLERY_SORT_OPTIONS.filter((entry) => entry.value !== "type");
const AUDIO_DUCK_VOLUME = Math.pow(10, -3 / 20);
const LIST_NAV_KEYS = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
const THUMBNAIL_SIZE_CONFIG = {
    small: { thumbWidth: 60, thumbHeight: 44, gap: 6, padding: 5, nameFont: 10, metaFont: 9 },
    medium: { thumbWidth: 72, thumbHeight: 54, gap: 8, padding: 6, nameFont: 11, metaFont: 10 },
    large: { thumbWidth: 88, thumbHeight: 66, gap: 10, padding: 8, nameFont: 12, metaFont: 10 },
};

export const INSPECT_OVERLAY_SHORTCUTS = Object.freeze([
    ["ArrowLeft / ArrowRight", "Cycle assets; step one frame in video compare"],
    ["Shift + ArrowLeft / ArrowRight", "Step -/+10 frames in video compare"],
    ["Ctrl + ArrowLeft / ArrowRight", "Step -/+1 second in video compare"],
    ["Space", "Play / Pause"],
    ["Right-drag video", "Scrub from current playhead"],
    ["1 / 2 / 3 / 0", "Monitor A / B / Both / Mute in audio compare"],
    ["Shift hold", "Temporarily flip A/B monitor in audio compare"],
    ["C", "Toggle Compare"],
    ["F / 0", "Fit"],
    ["+ / -", "Zoom"],
    ["Wheel", "Zoom / waveform zoom"],
    ["Esc", "Close overlay"],
]);

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
    warningBorder: "#9a7a42",
    dangerBorder: "#8f5f66",
};

function inputChromeCss({ minWidth = "0", padding = "6px 8px" } = {}) {
    return `background:${CHROME.panel};border:1px solid ${CHROME.border};border-radius:6px;color:${CHROME.text};padding:${padding};font-size:11px;min-width:${minWidth};box-shadow:inset 0 1px 0 rgba(255,255,255,0.03);`;
}

function actionButtonCss(variant = "primary") {
    const variants = {
        primary: `border:1px solid ${CHROME.accentBorder};background:${CHROME.accentSoft};color:#fff;`,
        subtle: `border:1px solid ${CHROME.border};background:${CHROME.panelRaised};color:${CHROME.textDim};`,
        active: `border:1px solid #7ea8c9;background:${CHROME.accent};color:#fff;`,
        danger: `border:1px solid ${CHROME.dangerBorder};background:#44292d;color:#efc0c4;`,
    };
    return `padding:6px 10px;border-radius:6px;${variants[variant] || variants.primary}cursor:pointer;font-size:11px;font-weight:600;box-shadow:inset 0 1px 0 rgba(255,255,255,0.04);`;
}

function menuChromeCss(minWidth = 160) {
    return `
        position: fixed; z-index: 10000;
        background: ${CHROME.panelRaised};
        border: 1px solid ${CHROME.borderStrong};
        border-radius: 8px;
        box-shadow: 0 12px 28px rgba(0,0,0,0.42);
        min-width: ${minWidth}px;
        padding: 6px 0;
        font-size: 11px;
    `;
}

function normalizeFolderName(folder) {
    return String(folder || "")
        .replace(/\\/g, "/")
        .split("/")
        .map((part) => part.trim())
        .filter(Boolean)
        .join("/");
}

function projectIdFromDir(projectDir) {
    return projectDir ? projectDir.split(/[/\\]/).pop() : "";
}

function buildAssetViewUrl(projectDir, sourcePath) {
    if (!projectDir || !sourcePath) return "";
    const dirName = projectIdFromDir(projectDir);
    const fileName = sourcePath.split(/[/\\]/).pop();
    const subPath = sourcePath.split(/[/\\]/).slice(0, -1).join("/");
    const subfolder = `sonder-projects/${dirName}/${subPath}`;
    return api.apiURL(`/view?filename=${encodeURIComponent(fileName)}&subfolder=${encodeURIComponent(subfolder)}&type=output`);
}

function buildThumbnailUrl(projectDir, assetId) {
    return api.apiURL(`/sonder-editor/project/${projectIdFromDir(projectDir)}/thumbnail/${assetId}`);
}

function buildWaveformUrl(projectDir, assetId) {
    return api.apiURL(`/sonder-editor/project/${projectIdFromDir(projectDir)}/waveform/${assetId}`);
}

export function loadMediaAsBlob(url, mediaEl) {
    if (!url || !mediaEl) return { cleanup: () => {} };
    let blobUrl = null;
    let aborted = false;
    fetch(url)
        .then((resp) => resp.blob())
        .then((blob) => {
            if (aborted) return;
            blobUrl = URL.createObjectURL(blob);
            mediaEl.src = blobUrl;
        })
        .catch(() => {
            if (!aborted) mediaEl.src = url;
        });
    return {
        cleanup() {
            aborted = true;
            if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = null; }
        },
    };
}

const OVERLAY_CAPTURE_KEYS = new Set([
    "Escape",
    " ",
    "Spacebar",
    "ArrowLeft",
    "ArrowRight",
    "ArrowUp",
    "ArrowDown",
    "Home",
    "End",
    "Delete",
    "Backspace",
    "?",
    "=",
    "+",
    "-",
    "_",
    "0",
    "1",
    "2",
    "3",
    "a",
    "A",
    "c",
    "C",
    "f",
    "F",
    "i",
    "I",
    "o",
    "O",
    "s",
    "S",
    "t",
    "T",
    "x",
    "X",
]);

function shouldCaptureOverlayShortcut(event) {
    const key = event?.key;
    if (!key) return false;
    if ((event.ctrlKey || event.metaKey) && (key === "z" || key === "Z" || key === "y" || key === "Y")) {
        return true;
    }
    return OVERLAY_CAPTURE_KEYS.has(key);
}

function assetKindLabel(type) {
    if (type === "video") return "Video";
    if (type === "image") return "Image";
    if (type === "audio") return "Audio";
    if (type === "artifact") return "Artifact";
    return "Asset";
}

function assetFallbackGlyph(type) {
    if (type === "video") return "V";
    if (type === "image") return "I";
    if (type === "audio") return "A";
    if (type === "artifact") return "R";
    return "A";
}

function assetDisplayName(asset) {
    return asset?.name || asset?.path?.split(/[/\\]/).pop() || "Untitled";
}

function assetIsMissing(asset) {
    return !!asset?.missing;
}

function isTrashed(asset) {
    return !!asset?.trashed_at;
}

function formatResolution(asset) {
    return asset?.width && asset?.height ? `${asset.width}x${asset.height}` : "-";
}

function formatDuration(asset) {
    if (Number.isFinite(asset?.frame_count) && asset.frame_count > 0) return `${asset.frame_count}f`;
    if (Number.isFinite(asset?.duration_sec) && asset.duration_sec > 0) {
        return `${asset.duration_sec.toFixed(2)}s`;
    }
    return "-";
}

function formatBytes(value) {
    const size = Number(value);
    if (!Number.isFinite(size) || size < 0) return "-";
    return `${Math.round(size)} B`;
}

function formatDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
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

function thumbnailSizeConfig(size) {
    return THUMBNAIL_SIZE_CONFIG[size] || THUMBNAIL_SIZE_CONFIG[DEFAULT_EDITOR_SETTINGS.gallery.thumbnailSize];
}

function formatGenerationValue(value) {
    if (value == null || value === "") return "-";
    if (typeof value === "number" || typeof value === "boolean") return String(value);
    if (typeof value === "string") return value;
    try {
        return JSON.stringify(value);
    } catch {
        return String(value);
    }
}

function assetExtension(asset) {
    const path = String(asset?.path || "");
    const match = path.match(/\.([^.\\/]+)$/);
    return match ? match[1].toLowerCase() : "";
}

function editorExportFor(asset) {
    const value = asset?.generation_params?.editor_export;
    return value && typeof value === "object" ? value : {};
}

function embeddedWorkflowFlag(asset) {
    const value = editorExportFor(asset).has_embedded_workflow;
    return typeof value === "boolean" ? value : null;
}

function workflowStatusLabel(asset) {
    const flag = embeddedWorkflowFlag(asset);
    if (flag === true) return "Embedded";
    if (flag === false) return "Not embedded";
    return "Unknown";
}

function workflowStatusMeta(asset) {
    const flag = embeddedWorkflowFlag(asset);
    if (flag === true) return "workflow";
    if (flag === false) return "no workflow";
    return "";
}

function parseAssetSearchQuery(rawQuery) {
    const result = { nameTerms: [], kindTerms: [], extTerms: [], trackedTerms: [], fieldTerms: [] };
    for (const token of String(rawQuery || "").trim().split(/\s+/).filter(Boolean)) {
        const lowerToken = token.toLowerCase();
        if (lowerToken.startsWith("kind:")) {
            const kind = lowerToken.slice(5).trim();
            if (kind) result.kindTerms.push(kind);
            continue;
        }
        if (lowerToken.startsWith("ext:")) {
            const ext = lowerToken.slice(4).replace(/^\./, "").trim();
            if (ext) result.extTerms.push(ext);
            continue;
        }
        if (lowerToken.startsWith("tracked:")) {
            const term = lowerToken.slice(8).trim();
            if (term) result.trackedTerms.push(term);
            continue;
        }
        if (lowerToken.startsWith("field:")) {
            const rawField = token.slice(6);
            const eqIndex = rawField.indexOf("=");
            if (eqIndex > 0) {
                const decode = (value) => {
                    try { return decodeURIComponent(value); } catch { return value; }
                };
                result.fieldTerms.push({
                    name: decode(rawField.slice(0, eqIndex)).toLowerCase(),
                    value: decode(rawField.slice(eqIndex + 1)).toLowerCase(),
                });
            }
            continue;
        }
        result.nameTerms.push(lowerToken);
    }
    return result;
}

function trackedMetadataEntries(asset) {
    const entries = asset?.generation_params?.editor_export?.tracked_metadata;
    return Array.isArray(entries) ? entries.filter((entry) => entry && typeof entry === "object") : [];
}

function trackedMetadataBlob(asset) {
    return trackedMetadataEntries(asset).map((entry) => {
        try {
            return JSON.stringify({
                label: entry.label || "",
                raw_widget_text: entry.raw_widget_text || "",
                fields: entry.fields || {},
            });
        } catch {
            return `${entry.label || ""} ${entry.raw_widget_text || ""}`;
        }
    }).join(" ").toLowerCase();
}

function trackedFieldMatches(asset, name, value) {
    for (const entry of trackedMetadataEntries(asset)) {
        const fields = entry.fields && typeof entry.fields === "object" ? entry.fields : {};
        for (const [fieldName, fieldValue] of Object.entries(fields)) {
            if (String(fieldName).toLowerCase() !== name) continue;
            if (formatGenerationValue(fieldValue).toLowerCase().includes(value)) return true;
        }
    }
    return false;
}

function canOpenWorkflowFor(asset) {
    if (editorExportFor(asset).has_embedded_workflow) return true;
    return new Set(["png", "mp4", "m4v", "mov", "mkv"]).has(assetExtension(asset));
}

function compareStrings(a, b) {
    return String(a || "").localeCompare(String(b || ""), undefined, { sensitivity: "base", numeric: true });
}

function folderCollapseKey(folderName) {
    const normalized = normalizeFolderName(folderName);
    return normalized || ROOT_FOLDER_COLLAPSE_KEY;
}

function assetImportedAt(asset) {
    const value = Date.parse(asset?.imported_at || "");
    return Number.isFinite(value) ? value : 0;
}

function assetDurationSortValue(asset) {
    if (Number.isFinite(asset?.frame_count) && asset.frame_count > 0) return asset.frame_count;
    if (Number.isFinite(asset?.duration_sec) && asset.duration_sec > 0) return asset.duration_sec;
    return 0;
}

function assetResolutionSortValue(asset) {
    const width = Number(asset?.width) || 0;
    const height = Number(asset?.height) || 0;
    return width * height;
}

function safeStorageGet(key) {
    try {
        return localStorage.getItem(key);
    } catch {
        return null;
    }
}

function safeStorageSet(key, value) {
    try {
        localStorage.setItem(key, value);
    } catch {
        // Ignore unavailable storage.
    }
}

function readStoredJson(key, fallback) {
    const raw = safeStorageGet(key);
    if (!raw) return fallback;
    try {
        return JSON.parse(raw);
    } catch {
        return fallback;
    }
}

async function readDroppedDirectoryFiles(dirEntry) {
    return await new Promise((resolve, reject) => {
        const reader = dirEntry.createReader();
        const entries = [];
        const readNext = () => {
            reader.readEntries((batch) => {
                if (!batch.length) {
                    Promise.all(entries.map((entry) => new Promise((fileResolve) => {
                        if (!entry?.isFile) {
                            fileResolve(null);
                            return;
                        }
                        entry.file((file) => fileResolve(file), () => fileResolve(null));
                    })))
                        .then((files) => resolve(files.filter(Boolean)))
                        .catch(reject);
                    return;
                }
                entries.push(...batch);
                readNext();
            }, reject);
        };
        readNext();
    });
}

async function collectDroppedFiles(dataTransfer) {
    const items = Array.from(dataTransfer?.items || []);
    const collected = [];
    let usedEntries = false;
    for (const item of items) {
        const entry = typeof item.webkitGetAsEntry === "function" ? item.webkitGetAsEntry() : null;
        if (entry?.isDirectory) {
            usedEntries = true;
            const files = await readDroppedDirectoryFiles(entry);
            for (const file of files) {
                collected.push({ file, folder: normalizeFolderName(entry.name || "") });
            }
        } else if (entry?.isFile) {
            const file = item.getAsFile?.();
            if (file) {
                usedEntries = true;
                collected.push({ file, folder: "" });
            }
        }
    }
    if (usedEntries) return collected;
    return Array.from(dataTransfer?.files || []).map((file) => ({ file, folder: "" }));
}

function dataTransferHasType(dataTransfer, type) {
    const types = dataTransfer?.types;
    if (!types || !type) return false;
    if (typeof types.includes === "function") return types.includes(type);
    if (typeof types.contains === "function") return types.contains(type);
    return Array.from(types).includes(type);
}

function makeMetaCell(label, value) {
    const displayValue = String(value ?? "-");
    const cell = style(document.createElement("div"), `padding:6px;border-radius:6px;background:rgba(255,255,255,0.03);border:1px solid transparent;min-width:0;`);
    const title = style(document.createElement("div"), `color:#7f8b96;margin-bottom:2px;font-size:10px;`);
    title.textContent = label;
    const content = style(document.createElement("div"), `color:#ececec;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
    content.textContent = displayValue;
    cell.addEventListener("mouseenter", () => {
        if (content.scrollWidth <= content.clientWidth) return;
        content.style.whiteSpace = "normal";
        content.style.wordBreak = "break-word";
        content.style.maxHeight = "140px";
        content.style.overflow = "auto";
        content.style.textOverflow = "clip";
    });
    cell.addEventListener("mouseleave", () => {
        content.style.whiteSpace = "nowrap";
        content.style.wordBreak = "";
        content.style.maxHeight = "";
        content.style.overflow = "hidden";
        content.style.textOverflow = "ellipsis";
    });
    cell.append(title, content);
    return cell;
}

function makeSectionTitle(label) {
    const title = style(document.createElement("div"), `color:#a9b8c4;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;`);
    title.textContent = label;
    return title;
}

export function mountSharedAssetGallery(container, options = {}) {
    const initialSettings = getEditorSettings();
    const initialInspectorSettings = initialSettings.inspector || DEFAULT_INSPECTOR_SETTINGS;
    const ownerId = typeof options.ownerId === "string" && options.ownerId
        ? options.ownerId
        : `sonder-gallery-${Math.random().toString(36).slice(2, 8)}`;
    const consumerId = (suffix) => `${ownerId}:${suffix}`;
    const state = {
        type: "all",
        query: "",
        selectedAssetId: "",
        selectedAssetIds: new Set(),
        focusedAssetId: "",
        allowAutoFocus: true,
        liveMedia: null,
        liveMediaCleanup: null,
        dropFolder: "",
        destroyed: false,
        collapsedFolders: new Set(),
        inspectorCollapsed: !!initialSettings.gallery.inspectorCollapsed,
        artifactInspectorExpanded: !!initialSettings.gallery.artifactInspectorExpanded,
        manageMode: false,
        sortMode: initialSettings.gallery.sortMode || DEFAULT_SORT_MODE,
        thumbnailSize: initialSettings.gallery.thumbnailSize || DEFAULT_EDITOR_SETTINGS.gallery.thumbnailSize,
        storageProjectId: "",
        contextMenuEl: null,
        contextMenuCleanup: null,
        showingUsagesFor: "",
        usageLoading: false,
        usageError: "",
        usageData: null,
        replaceAssetId: "",
        lastInteractedAt: 0,
        overlayState: {
            open: false,
            assetId: "",
            zoomLevel: 1,
            panX: 0,
            panY: 0,
            overlayEl: null,
            cleanupFns: [],
            compareMode: false,
            showMetadata: false,
            compareLeftAssetId: "",
            compareRightAssetId: "",
            comparePickerQuery: "",
            comparePickerSortMode: DEFAULT_SORT_MODE,
            compareLayout: initialInspectorSettings.compareLayout || DEFAULT_INSPECTOR_SETTINGS.compareLayout,
            sideBySideLinkZoom: initialInspectorSettings.sideBySideLinkZoom !== false,
            audioCompareWaveformLayout: initialInspectorSettings.audioCompareWaveformLayout || DEFAULT_INSPECTOR_SETTINGS.audioCompareWaveformLayout,
            audioCompareMonitor: initialInspectorSettings.audioCompareMonitor || DEFAULT_INSPECTOR_SETTINGS.audioCompareMonitor,
            audioTempFlip: false,
            dividerRatio: 0.5,
            audioFocus: "none",
            showWaveform: false,
            togglePlayback: null,
            stepVideoCompare: null,
            videoCompareFps: 30,
            applyAudioMonitor: null,
            sideBySideTransforms: {
                a: { zoomLevel: 1, panX: 0, panY: 0 },
                b: { zoomLevel: 1, panX: 0, panY: 0 },
            },
        },
    };
    const data = { assets: [], folders: [] };
    const root = style(document.createElement("div"), `display:flex;flex-direction:column;gap:8px;flex:1 1 auto;min-width:0;min-height:0;width:100%;height:100%;box-sizing:border-box;overflow:hidden;`);
    container.appendChild(root);

    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.multiple = true;
    fileInput.style.display = "none";
    root.appendChild(fileInput);

    const replaceInput = document.createElement("input");
    replaceInput.type = "file";
    replaceInput.style.display = "none";
    root.appendChild(replaceInput);

    const controls = style(document.createElement("div"), `display:flex;gap:6px;align-items:center;min-width:0;flex-wrap:wrap;`);
    const searchInput = style(document.createElement("input"), `flex:1 1 180px;${inputChromeCss({ minWidth: "120px" })}`);
    searchInput.type = "search";
    searchInput.placeholder = "Search assets (kind:/ext:/tracked:/field:)";
    controls.appendChild(searchInput);

    const sortSelect = style(document.createElement("select"), `flex:0 0 auto;${inputChromeCss({ minWidth: "110px" })}`);
    for (const option of SORT_OPTIONS) {
        const optionEl = document.createElement("option");
        optionEl.value = option.value;
        optionEl.textContent = option.label;
        sortSelect.appendChild(optionEl);
    }
    controls.appendChild(sortSelect);

    const makeActionButton = (variant = "primary") => style(document.createElement("button"), actionButtonCss(variant));

    const inspectorBtn = makeActionButton("subtle");
    controls.appendChild(inspectorBtn);

    const importBtn = makeActionButton();
    importBtn.textContent = "Import";
    importBtn.addEventListener("click", () => fileInput.click());
    controls.appendChild(importBtn);

    const folderBtn = makeActionButton();
    folderBtn.textContent = "+ Folder";
    controls.appendChild(folderBtn);

    const refreshBtn = makeActionButton("subtle");
    refreshBtn.textContent = "Refresh";
    refreshBtn.addEventListener("click", async () => {
        hideContextMenu();
        await options.onRefresh?.();
    });
    controls.appendChild(refreshBtn);

    const manageBtn = makeActionButton();
    controls.appendChild(manageBtn);

    root.appendChild(controls);

    const tabsRow = style(document.createElement("div"), `display:flex;flex-wrap:wrap;gap:6px;`);
    root.appendChild(tabsRow);

    const content = style(document.createElement("div"), `display:grid;grid-template-columns:minmax(0,1.2fr) minmax(260px,1fr);grid-template-rows:minmax(0,1fr);gap:8px;align-items:stretch;min-height:0;flex:1 1 0;overflow:hidden;`);
    root.appendChild(content);

    const listPane = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:6px;min-height:0;overflow:hidden;`);
    const bulkToolbarHost = style(document.createElement("div"), `display:none;flex:0 0 auto;`);
    const listScroller = style(document.createElement("div"), `overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding-right:2px;min-height:0;outline:none;${options.maxListHeight ? `max-height:${options.maxListHeight}px;` : "flex:1 1 0;"}`);
    listScroller.tabIndex = 0;
    listPane.append(bulkToolbarHost, listScroller);

    const detailPane = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:8px;padding:8px;border-radius:8px;background:rgba(255,255,255,0.03);border:1px solid ${CHROME.border};min-height:0;overflow:auto;`);
    content.append(listPane, detailPane);

    const folderListId = `sonder-gallery-folders-${Math.random().toString(36).slice(2)}`;
    const folderList = document.createElement("datalist");
    folderList.id = folderListId;
    root.appendChild(folderList);

    function currentProjectDir() {
        return options.getProjectDir?.() || "";
    }

    function currentProjectId() {
        return projectIdFromDir(currentProjectDir()) || "default";
    }

    function storageKey(suffix) {
        return `sonder-gallery-${currentProjectId()}-${suffix}`;
    }

    function ensureProjectPrefs() {
        const projectId = currentProjectId();
        if (state.storageProjectId === projectId) return;
        state.storageProjectId = projectId;
        const storedCollapsed = readStoredJson(storageKey("collapsed-folders"), []);
        state.collapsedFolders = new Set(
            Array.isArray(storedCollapsed)
                ? storedCollapsed.map(normalizeFolderName).filter(Boolean)
                : [],
        );
        state.manageMode = !!readStoredJson(storageKey("manage-mode"), false);
        const migratedSettings = migrateLegacyGalleryProjectPrefs(projectId);
        applyGallerySettings(migratedSettings, { skipRender: true });
    }

    function persistCollapsedFolders() {
        safeStorageSet(storageKey("collapsed-folders"), JSON.stringify(Array.from(state.collapsedFolders)));
    }

    function persistInspectorCollapsed() {
        updateEditorSettings({
            gallery: {
                inspectorCollapsed: !!state.inspectorCollapsed,
            },
        });
    }

    function persistArtifactInspectorExpanded() {
        updateEditorSettings({
            gallery: {
                artifactInspectorExpanded: !!state.artifactInspectorExpanded,
            },
        });
    }

    function persistManageMode() {
        safeStorageSet(storageKey("manage-mode"), JSON.stringify(!!state.manageMode));
    }

    function persistSortMode() {
        updateEditorSettings({
            gallery: {
                sortMode: state.sortMode,
            },
        });
    }

    function persistInspectorSetting(key, value) {
        updateEditorSettings({
            inspector: {
                [key]: value,
            },
        });
    }

    function applyGallerySettings(settings, { skipRender = false } = {}) {
        const nextSettings = settings || getEditorSettings();
        const nextSort = SORT_OPTIONS.some((entry) => entry.value === nextSettings?.gallery?.sortMode)
            ? nextSettings.gallery.sortMode
            : DEFAULT_SORT_MODE;
        state.sortMode = nextSort;
        state.inspectorCollapsed = !!nextSettings?.gallery?.inspectorCollapsed;
        state.artifactInspectorExpanded = !!nextSettings?.gallery?.artifactInspectorExpanded;
        state.thumbnailSize = nextSettings?.gallery?.thumbnailSize || DEFAULT_EDITOR_SETTINGS.gallery.thumbnailSize;
        const inspector = nextSettings?.inspector || DEFAULT_INSPECTOR_SETTINGS;
        state.overlayState.compareLayout = inspector.compareLayout || DEFAULT_INSPECTOR_SETTINGS.compareLayout;
        state.overlayState.sideBySideLinkZoom = inspector.sideBySideLinkZoom !== false;
        state.overlayState.audioCompareWaveformLayout = inspector.audioCompareWaveformLayout || DEFAULT_INSPECTOR_SETTINGS.audioCompareWaveformLayout;
        state.overlayState.audioCompareMonitor = inspector.audioCompareMonitor || DEFAULT_INSPECTOR_SETTINGS.audioCompareMonitor;
        updateControlState();
        if (!skipRender && !state.destroyed) {
            render();
        }
    }

    function updateFolderOptions() {
        folderList.innerHTML = "";
        for (const folder of allFolders().filter(Boolean)) {
            const option = document.createElement("option");
            option.value = folder;
            folderList.appendChild(option);
        }
    }

    function updateControlState() {
        sortSelect.value = state.sortMode;
        inspectorBtn.textContent = state.inspectorCollapsed ? "Show Inspector" : "Hide Inspector";
        manageBtn.textContent = state.manageMode ? "Done" : "Manage";
        manageBtn.style.cssText = actionButtonCss(state.manageMode ? "active" : "subtle");
    }

    const unsubscribeSettings = subscribeEditorSettings((settings) => {
        applyGallerySettings(settings);
    });

    function setBusyButton(btn, isBusy, busyLabel, idleLabel) {
        btn.disabled = isBusy;
        btn.textContent = isBusy ? busyLabel : idleLabel;
        btn.style.opacity = isBusy ? "0.75" : "1";
    }

    function queueResize() {
        options.onRequestResize?.();
    }

    function setDropFolderHighlight(folderName = "") {
        state.dropFolder = folderName;
        for (const el of root.querySelectorAll("[data-folder-drop]")) {
            el.style.borderColor = el.dataset.folderDrop === state.dropFolder ? CHROME.accentBorder : "transparent";
        }
    }

    function clearDropFolderHighlight() {
        setDropFolderHighlight("");
    }

    function destroyLiveMedia() {
        if (state.liveMediaCleanup) {
            state.liveMediaCleanup();
            state.liveMediaCleanup = null;
        }
        if (!state.liveMedia) return;
        state.liveMedia.pause?.();
        state.liveMedia.removeAttribute?.("src");
        state.liveMedia.load?.();
        state.liveMedia.remove();
        state.liveMedia = null;
    }

    function hideContextMenu() {
        if (state.contextMenuCleanup) {
            state.contextMenuCleanup();
            state.contextMenuCleanup = null;
        }
        if (state.contextMenuEl) {
            state.contextMenuEl.remove();
            state.contextMenuEl = null;
        }
    }

    function showContextMenu(x, y, items) {
        hideContextMenu();

        const menu = style(document.createElement("div"), `
            left: ${x}px; top: ${y}px;
            ${menuChromeCss(160)}
        `);

        for (const item of items) {
            if (!item) continue;
            if (item.type === "separator") {
                menu.appendChild(style(document.createElement("div"), `height:1px;background:${CHROME.border};margin:4px 0;`));
                continue;
            }
            const row = document.createElement("div");
            row.textContent = item.label;
            const isDisabled = !!item.disabled;
            row.style.cssText = `
                padding: 6px 14px; cursor: ${isDisabled ? "default" : "pointer"};
                color: ${isDisabled ? CHROME.textMuted : (item.danger ? "#efc0c4" : CHROME.text)};
            `;
            if (!isDisabled) {
                row.addEventListener("mouseenter", () => {
                    row.style.background = CHROME.panelRaisedHover;
                });
                row.addEventListener("mouseleave", () => {
                    row.style.background = "transparent";
                });
                row.addEventListener("click", () => {
                    hideContextMenu();
                    item.action?.();
                });
            }
            menu.appendChild(row);
        }

        document.body.appendChild(menu);
        state.contextMenuEl = menu;

        requestAnimationFrame(() => {
            const rect = menu.getBoundingClientRect();
            menu.style.left = `${Math.max(4, Math.min(x, window.innerWidth - rect.width - 4))}px`;
            menu.style.top = `${Math.max(4, Math.min(y, window.innerHeight - rect.height - 4))}px`;
        });

        const closeHandler = (event) => {
            if (!menu.contains(event.target)) hideContextMenu();
        };
        const scrollHandler = () => hideContextMenu();
        const escKeyOff = registerKeyboardConsumer({
            id: consumerId("ctxmenu"),
            priority: KEY_PRIORITY.OVERLAY,
            keydown: (event) => {
                if (event.key === "Escape") { hideContextMenu(); return true; }
                return false;
            },
        });
        state.contextMenuCleanup = () => {
            document.removeEventListener("mousedown", closeHandler);
            window.removeEventListener("scroll", scrollHandler, true);
            escKeyOff();
        };
        setTimeout(() => {
            document.addEventListener("mousedown", closeHandler);
            window.addEventListener("scroll", scrollHandler, true);
        }, 10);
    }

    function allFolders() {
        const folders = new Set((data.folders || []).map(normalizeFolderName).filter(Boolean));
        for (const asset of data.assets) {
            if (isTrashed(asset)) continue;
            const folder = normalizeFolderName(asset.folder || "");
            if (folder) folders.add(folder);
        }
        if (!folders.size && !data.assets.some((asset) => !isTrashed(asset))) return [];
        return ["", ...Array.from(folders).sort(compareStrings)];
    }

    function isFolderCollapsed(folderName) {
        return state.collapsedFolders.has(folderCollapseKey(folderName));
    }

    function isAncestorCollapsed(folderName) {
        const parts = normalizeFolderName(folderName).split("/");
        if (parts.length <= 1 || !parts[0]) return false;
        for (let i = 1; i < parts.length; i++) {
            if (isFolderCollapsed(parts.slice(0, i).join("/"))) return true;
        }
        return false;
    }

    function toggleFolderCollapsed(folderName) {
        const key = folderCollapseKey(folderName);
        const willCollapse = !state.collapsedFolders.has(key);
        if (!willCollapse) {
            state.collapsedFolders.delete(key);
        } else {
            state.collapsedFolders.add(key);
            const selected = data.assets.find((asset) => asset.asset_id === state.selectedAssetId);
            const hidesSelected = selected && (
                (key === TRASH_FOLDER_COLLAPSE_KEY && isTrashed(selected))
                || folderContainsPath(folderName, selected.folder)
            );
            if (hidesSelected) {
                const nextVisibleAsset = navigableAssets()[0] || null;
                applySelectionState(nextVisibleAsset ? [nextVisibleAsset.asset_id] : [], nextVisibleAsset?.asset_id || "");
            }
        }
        persistCollapsedFolders();
        render();
    }

    function revealAssetFolder(asset) {
        if (!asset) return false;
        let changed = false;
        if (isTrashed(asset)) {
            if (state.collapsedFolders.has(TRASH_FOLDER_COLLAPSE_KEY)) {
                state.collapsedFolders.delete(TRASH_FOLDER_COLLAPSE_KEY);
                changed = true;
            }
        } else {
            const parts = normalizeFolderName(asset.folder || "").split("/").filter(Boolean);
            const folderChain = [""];
            for (let i = 1; i <= parts.length; i++) {
                folderChain.push(parts.slice(0, i).join("/"));
            }
            for (const folderName of folderChain) {
                const key = folderCollapseKey(folderName);
                if (state.collapsedFolders.has(key)) {
                    state.collapsedFolders.delete(key);
                    changed = true;
                }
            }
        }
        if (changed) persistCollapsedFolders();
        return changed;
    }

    function assetMatchesCurrentFilter(asset) {
        if (!asset) return false;
        if (state.type !== "all" && asset.asset_type !== state.type) return false;
        const query = parseAssetSearchQuery(state.query);
        return assetMatchesParsedQuery(asset, query);
    }

    function assetMatchesParsedQuery(asset, query) {
        if (query.kindTerms.length) {
            if (asset.asset_type !== "artifact") return false;
            const artifactKind = String(asset.artifact_kind || "").toLowerCase();
            if (!query.kindTerms.every((term) => artifactKind === term)) return false;
        }
        if (query.extTerms.length) {
            const ext = assetExtension(asset);
            if (!query.extTerms.every((term) => ext === term)) return false;
        }
        if (query.trackedTerms.length) {
            const blob = trackedMetadataBlob(asset);
            if (!query.trackedTerms.every((term) => blob.includes(term))) return false;
        }
        if (query.fieldTerms.length) {
            if (!query.fieldTerms.every((term) => trackedFieldMatches(asset, term.name, term.value))) return false;
        }
        if (!query.nameTerms.length) return true;
        const name = assetDisplayName(asset).toLowerCase();
        return query.nameTerms.every((term) => name.includes(term));
    }

    function addSearchToken(token) {
        const normalized = String(token || "").trim();
        if (!normalized) return;
        const tokens = String(state.query || "").trim().split(/\s+/).filter(Boolean);
        if (!tokens.includes(normalized)) tokens.push(normalized);
        state.query = tokens.join(" ");
        searchInput.value = state.query;
        state.allowAutoFocus = true;
        render();
    }

    function removeSearchToken(token) {
        const normalized = String(token || "").trim();
        if (!normalized) return;
        const tokens = String(state.query || "").trim().split(/\s+/).filter(Boolean);
        const nextTokens = tokens.filter((entry) => entry !== normalized);
        if (nextTokens.length === tokens.length) return;
        state.query = nextTokens.join(" ");
        searchInput.value = state.query;
        state.allowAutoFocus = true;
        render();
    }

    function toggleSearchToken(token) {
        if (searchHasToken(token)) {
            removeSearchToken(token);
        } else {
            addSearchToken(token);
        }
    }

    function fieldSearchToken(key, value) {
        return `field:${encodeURIComponent(String(key || ""))}=${encodeURIComponent(String(value || ""))}`;
    }

    function activeSearchTokens() {
        return new Set(String(state.query || "").trim().split(/\s+/).filter(Boolean));
    }

    function searchHasToken(token) {
        return activeSearchTokens().has(String(token || "").trim());
    }

    function sortAssetsByMode(assets, sortMode) {
        return [...assets].sort((left, right) => {
            if (sortMode === "oldest") {
                return assetImportedAt(left) - assetImportedAt(right) || compareStrings(assetDisplayName(left), assetDisplayName(right));
            }
            if (sortMode === "name") {
                return compareStrings(assetDisplayName(left), assetDisplayName(right)) || (assetImportedAt(right) - assetImportedAt(left));
            }
            if (sortMode === "type") {
                return compareStrings(left.asset_type, right.asset_type) || compareStrings(assetDisplayName(left), assetDisplayName(right));
            }
            if (sortMode === "duration") {
                return assetDurationSortValue(right) - assetDurationSortValue(left) || compareStrings(assetDisplayName(left), assetDisplayName(right));
            }
            if (sortMode === "resolution") {
                return assetResolutionSortValue(right) - assetResolutionSortValue(left) || compareStrings(assetDisplayName(left), assetDisplayName(right));
            }
            return assetImportedAt(right) - assetImportedAt(left) || compareStrings(assetDisplayName(left), assetDisplayName(right));
        });
    }

    function sortAssets(assets) {
        return sortAssetsByMode(assets, state.sortMode);
    }

    function filteredAssets() {
        return sortAssets(data.assets.filter((asset) => !isTrashed(asset) && assetMatchesCurrentFilter(asset)));
    }

    function trashedAssets() {
        return data.assets
            .filter((asset) => isTrashed(asset) && assetMatchesCurrentFilter(asset))
            .sort((left, right) => {
                return assetImportedAt({ imported_at: right.trashed_at }) - assetImportedAt({ imported_at: left.trashed_at })
                    || compareStrings(assetDisplayName(left), assetDisplayName(right));
            });
    }

    function folderAssets(folderName, assets) {
        return assets.filter((asset) => normalizeFolderName(asset.folder || "") === normalizeFolderName(folderName));
    }

    function visibleNavigableAssets(assets) {
        return assets.filter((asset) => !isFolderCollapsed(asset.folder) && !isAncestorCollapsed(asset.folder));
    }

    function visibleTrashedAssets() {
        return isFolderCollapsed(TRASH_FOLDER_COLLAPSE_KEY) ? [] : trashedAssets();
    }

    function navigableAssets() {
        return [...visibleNavigableAssets(filteredAssets()), ...visibleTrashedAssets()];
    }

    function selectedAssetIdsList() {
        const validIds = new Set(data.assets.map((asset) => asset.asset_id));
        return Array.from(state.selectedAssetIds).filter((assetId) => validIds.has(assetId));
    }

    function selectedAssets() {
        const ids = new Set(selectedAssetIdsList());
        return data.assets.filter((asset) => ids.has(asset.asset_id));
    }

    function normalizeSelection(assetIds, primaryAssetId = "") {
        const validIds = new Set(data.assets.map((asset) => asset.asset_id));
        const nextIds = [];
        const seen = new Set();

        for (const rawId of assetIds || []) {
            const assetId = String(rawId || "").trim();
            if (!assetId || !validIds.has(assetId) || seen.has(assetId)) continue;
            nextIds.push(assetId);
            seen.add(assetId);
        }

        let primaryId = String(primaryAssetId || "").trim();
        if (primaryId && validIds.has(primaryId) && !seen.has(primaryId)) {
            nextIds.push(primaryId);
            seen.add(primaryId);
        }
        if (!seen.has(primaryId)) {
            primaryId = nextIds[nextIds.length - 1] || "";
        }

        return { ids: nextIds, primaryId };
    }

    function applySelectionState(assetIds, primaryAssetId = "") {
        const { ids, primaryId } = normalizeSelection(assetIds, primaryAssetId);
        state.selectedAssetIds = new Set(ids);
        state.selectedAssetId = primaryId;
        state.focusedAssetId = primaryId || ids[0] || "";
        state.allowAutoFocus = ids.length > 0;
        return data.assets.find((item) => item.asset_id === state.selectedAssetId) || null;
    }

    function selectedAsset() {
        return data.assets.find((item) => item.asset_id === state.selectedAssetId) || null;
    }

    function ensureFocusedAsset(assets) {
        const navigable = Array.isArray(assets) ? assets : navigableAssets();
        if (!navigable.length) {
            state.focusedAssetId = "";
            return navigable;
        }
        if (navigable.some((asset) => asset.asset_id === state.focusedAssetId)) return navigable;
        if (state.allowAutoFocus) state.focusedAssetId = navigable[0].asset_id;
        return navigable;
    }

    function applySelection(assetIds, primaryAssetId, options = {}) {
        const { focusList = false, scrollIntoView = false } = options;
        const asset = applySelectionState(assetIds, primaryAssetId);

        if (state.selectedAssetIds.size > 1) {
            clearUsageView();
            render();
        } else if (!asset) {
            clearUsageView();
            render();
        } else if (state.showingUsagesFor && state.showingUsagesFor !== asset.asset_id) {
            void openUsageView(asset);
        } else {
            render();
        }

        if (focusList) {
            requestAnimationFrame(() => {
                listScroller.focus({ preventScroll: true });
            });
        }
        if (scrollIntoView && asset) {
            scrollAssetIntoView(asset.asset_id);
        }
        return asset;
    }

    function selectAsset(assetId, options = {}) {
        return applySelection(assetId ? [assetId] : [], assetId, options);
    }

    function revealAsset(assetId, options = {}) {
        const asset = data.assets.find((entry) => entry.asset_id === assetId);
        if (!asset) return null;

        if (state.type !== "all" && state.type !== asset.asset_type) {
            state.type = asset.asset_type;
        }
        if (!assetMatchesCurrentFilter(asset) && state.query) {
            state.query = "";
            searchInput.value = "";
        }
        if (options.openInspector && state.inspectorCollapsed) {
            state.inspectorCollapsed = false;
            persistInspectorCollapsed();
        }
        if (options.clearUsageView !== false) {
            clearUsageView();
        }
        revealAssetFolder(asset);
        return applySelection([asset.asset_id], asset.asset_id, {
            focusList: options.focusList !== false,
            scrollIntoView: options.scrollIntoView !== false,
        });
    }

    function toggleAssetSelection(assetId, options = {}) {
        const nextIds = new Set(selectedAssetIdsList());
        if (nextIds.has(assetId)) {
            nextIds.delete(assetId);
        } else {
            nextIds.add(assetId);
        }
        const ids = Array.from(nextIds);
        const primaryId = nextIds.has(assetId) ? assetId : (ids[ids.length - 1] || "");
        return applySelection(ids, primaryId, options);
    }

    function selectAssetRange(assetId, assets, options = {}) {
        const navigableAssets = visibleNavigableAssets(assets);
        const anchorId = state.selectedAssetId || state.focusedAssetId;
        if (!anchorId) return selectAsset(assetId, options);

        const startIndex = navigableAssets.findIndex((asset) => asset.asset_id === anchorId);
        const endIndex = navigableAssets.findIndex((asset) => asset.asset_id === assetId);
        if (startIndex < 0 || endIndex < 0) return selectAsset(assetId, options);

        const [from, to] = startIndex <= endIndex
            ? [startIndex, endIndex]
            : [endIndex, startIndex];
        const rangeIds = navigableAssets.slice(from, to + 1).map((asset) => asset.asset_id);
        return applySelection(rangeIds, assetId, options);
    }

    function handleAssetActivation(assetId, event, assets, options = {}) {
        if ((event?.ctrlKey || event?.metaKey) && !event?.shiftKey) {
            return toggleAssetSelection(assetId, options);
        }
        if (event?.shiftKey) {
            return selectAssetRange(assetId, assets, options);
        }
        if (state.manageMode) {
            return toggleAssetSelection(assetId, options);
        }
        return selectAsset(assetId, options);
    }

    function clearSelection(options = {}) {
        const { renderNow = true } = options;
        state.selectedAssetIds = new Set();
        state.selectedAssetId = "";
        state.focusedAssetId = "";
        state.allowAutoFocus = false;
        clearUsageView();
        if (renderNow) render();
    }

    function reduceSelectionToPrimary(options = {}) {
        const currentIds = selectedAssetIdsList();
        const primaryId = currentIds.includes(state.selectedAssetId)
            ? state.selectedAssetId
            : (currentIds[currentIds.length - 1] || "");
        if (!primaryId) {
            clearSelection(options);
            return null;
        }
        return applySelection([primaryId], primaryId, options);
    }

    function updateLayout() {
        const width = root.getBoundingClientRect().width || 0;
        const singleColumn = state.inspectorCollapsed || (width > 0 && width < 520);
        if (singleColumn && !state.inspectorCollapsed) {
            content.style.gridTemplateColumns = "minmax(0,1fr)";
            content.style.gridTemplateRows = "minmax(0,1.5fr) minmax(0,1fr)";
            detailPane.style.display = "flex";
        } else if (singleColumn) {
            content.style.gridTemplateColumns = "minmax(0,1fr)";
            content.style.gridTemplateRows = "minmax(0,1fr)";
            detailPane.style.display = "none";
        } else {
            content.style.gridTemplateColumns = "minmax(0,1.2fr) minmax(260px,1fr)";
            content.style.gridTemplateRows = "minmax(0,1fr)";
            detailPane.style.display = "flex";
        }
    }

    function updateAsset(updatedAsset) {
        if (!updatedAsset?.asset_id) return;
        const idx = data.assets.findIndex((asset) => asset.asset_id === updatedAsset.asset_id);
        if (idx >= 0) {
            data.assets[idx] = { ...data.assets[idx], ...updatedAsset };
        }
        const folder = normalizeFolderName(updatedAsset.folder || "");
        if (folder && !data.folders.includes(folder)) {
            data.folders = [...data.folders, folder].sort(compareStrings);
        }
    }

    function removeAssetsByIds(assetIds) {
        const ids = new Set((assetIds || []).filter(Boolean));
        if (!ids.size) return;
        data.assets = data.assets.filter((asset) => !ids.has(asset.asset_id));
        const nextSelectedIds = selectedAssetIdsList().filter((assetId) => !ids.has(assetId));
        const nextPrimaryId = ids.has(state.selectedAssetId)
            ? (nextSelectedIds[nextSelectedIds.length - 1] || "")
            : state.selectedAssetId;
        applySelectionState(nextSelectedIds, nextPrimaryId);
        if (!state.selectedAssetId && ids.has(state.focusedAssetId)) state.focusedAssetId = "";
        if (ids.has(state.showingUsagesFor)) clearUsageView();
        state.allowAutoFocus = true;
    }

    function folderContainsPath(folderName, candidate) {
        const normalizedFolder = normalizeFolderName(folderName);
        const normalizedCandidate = normalizeFolderName(candidate);
        if (!normalizedFolder || !normalizedCandidate) return false;
        return normalizedCandidate === normalizedFolder || normalizedCandidate.startsWith(`${normalizedFolder}/`);
    }

    function renameFolderPath(folderName, oldFolder, newFolder) {
        const normalizedFolder = normalizeFolderName(folderName);
        const normalizedOld = normalizeFolderName(oldFolder);
        const normalizedNew = normalizeFolderName(newFolder);
        if (!normalizedFolder || !normalizedOld) return normalizedFolder;
        if (normalizedFolder === normalizedOld) return normalizedNew;
        if (normalizedFolder.startsWith(`${normalizedOld}/`)) {
            return `${normalizedNew}/${normalizedFolder.slice(normalizedOld.length).replace(/^\/+/, "")}`.replace(/^\/+|\/+$/g, "");
        }
        return normalizedFolder;
    }

    function renameFolderLocally(oldFolder, newFolder, nextFolders = null) {
        data.assets = data.assets.map((asset) => ({
            ...asset,
            folder: renameFolderPath(asset.folder || "", oldFolder, newFolder),
        }));
        if (Array.isArray(nextFolders)) {
            data.folders = nextFolders.map(normalizeFolderName).filter(Boolean).sort(compareStrings);
        } else {
            data.folders = data.folders
                .map((folder) => renameFolderPath(folder, oldFolder, newFolder))
                .filter(Boolean)
                .sort(compareStrings);
        }
    }

    function removeFolderLocally(folderName) {
        data.folders = data.folders
            .filter((folder) => !folderContainsPath(folderName, folder))
            .sort(compareStrings);
    }

    function folderAssetsRecursive(folderName) {
        return data.assets.filter((asset) => folderContainsPath(folderName, asset.folder || ""));
    }

    function resetUsageState(assetId = "") {
        state.showingUsagesFor = assetId;
        state.usageLoading = false;
        state.usageError = "";
        state.usageData = null;
    }

    function clearUsageView() {
        resetUsageState("");
    }

    function summarizeUsageTypes(usages) {
        const counts = {
            clip: 0,
            audio_track: 0,
            guide_frame: 0,
            generation_job: 0,
        };
        for (const usage of usages || []) {
            if (Object.prototype.hasOwnProperty.call(counts, usage?.type)) {
                counts[usage.type] += 1;
            }
        }
        counts.total = counts.clip + counts.audio_track + counts.guide_frame + counts.generation_job;
        return counts;
    }

    function usageTypeLabel(type) {
        if (type === "clip") return "Clip";
        if (type === "audio_track") return "Audio Track";
        if (type === "guide_frame") return "Guide Frame";
        if (type === "generation_job") return "Generation Job";
        return "Usage";
    }

    function usagePositionLabel(usage) {
        if (!usage) return "";
        if (usage.type === "clip") {
            return `Track ${usage.track_index || 0} | ${usage.start_frame || 0}-${usage.end_frame || 0}`;
        }
        if (usage.type === "audio_track") {
            return `Lane ${usage.lane_index || 0} | ${usage.start_frame || 0}-${usage.end_frame || 0}`;
        }
        if (usage.type === "guide_frame") {
            return `Frame ${usage.frame_index ?? 0}`;
        }
        if (usage.type === "generation_job") {
            return `${usage.status || "pending"} | ${usage.job_id || "job"}`;
        }
        return "";
    }

    function groupUsagesByScene(usages) {
        const groups = new Map();
        for (const usage of usages || []) {
            const key = usage.scene_name || "Project Queue";
            if (!groups.has(key)) groups.set(key, []);
            groups.get(key).push(usage);
        }
        return Array.from(groups.entries()).map(([sceneName, items]) => ({ sceneName, items }));
    }

    async function openUsageView(asset) {
        if (!asset?.asset_id || !options.onGetAssetUsages) return;
        resetUsageState(asset.asset_id);
        state.usageLoading = true;
        render();
        try {
            const usage = await options.onGetAssetUsages(asset.asset_id);
            if (state.destroyed || state.showingUsagesFor !== asset.asset_id) return;
            state.usageData = usage || {
                asset_id: asset.asset_id,
                usages: [],
                usage_count: 0,
            };
        } catch (error) {
            if (state.destroyed || state.showingUsagesFor !== asset.asset_id) return;
            state.usageError = error?.message || "Failed to load usage.";
        } finally {
            if (state.showingUsagesFor === asset.asset_id) {
                state.usageLoading = false;
                render();
            }
        }
    }

    function scrollAssetIntoView(assetId) {
        if (!assetId) return;
        requestAnimationFrame(() => {
            const row = Array.from(root.querySelectorAll("[data-asset-row]"))
                .find((el) => el.dataset.assetRow === assetId);
            row?.scrollIntoView({ block: "nearest" });
        });
    }

    async function applyAssetUpdate(asset, updates) {
        if (!asset?.asset_id) return;
        const normalizedUpdates = { ...updates };
        if (Object.prototype.hasOwnProperty.call(normalizedUpdates, "folder")) {
            normalizedUpdates.folder = normalizeFolderName(normalizedUpdates.folder);
        }
        const updated = await options.onUpdateAsset?.(asset.asset_id, normalizedUpdates);
        updateAsset({ ...asset, ...normalizedUpdates, ...(updated || {}) });
        clearUsageView();
        render();
    }

    function summarizeAssetTypes(assets) {
        return assets.reduce((counts, asset) => {
            if (asset?.asset_type === "video") counts.video += 1;
            else if (asset?.asset_type === "image") counts.image += 1;
            else if (asset?.asset_type === "audio") counts.audio += 1;
            else if (asset?.asset_type === "artifact") counts.artifact += 1;
            return counts;
        }, { video: 0, image: 0, audio: 0, artifact: 0 });
    }

    function aggregateDurationLabel(assets) {
        const totalSeconds = assets.reduce((sum, asset) => {
            const durationSec = Number(asset?.duration_sec);
            return sum + (Number.isFinite(durationSec) && durationSec > 0 ? durationSec : 0);
        }, 0);
        return totalSeconds > 0 ? `${totalSeconds.toFixed(2)}s` : "-";
    }

    function commonFolderForAssets(assets) {
        if (!assets.length) return "";
        const firstFolder = normalizeFolderName(assets[0].folder);
        return assets.every((asset) => normalizeFolderName(asset.folder) === firstFolder) ? firstFolder : "";
    }

    async function handleBulkMove(assetIds = selectedAssetIdsList(), event) {
        const ids = normalizeSelection(assetIds, state.selectedAssetId).ids;
        if (!ids.length) return;
        if (ids.length === 1) {
            const asset = data.assets.find((entry) => entry.asset_id === ids[0]);
            if (asset) {
                await handleAssetMoveToFolder(asset, event);
            }
            return;
        }
        if (!options.onBulkMoveAssets) return;

        const assets = data.assets.filter((asset) => ids.includes(asset.asset_id));
        const currentFolder = commonFolderForAssets(assets);
        showFolderPicker(event, currentFolder, async (folder) => {
            try {
                await options.onBulkMoveAssets(ids, folder);
                clearUsageView();
                await options.onRefresh?.();
            } catch (error) {
                console.warn("[Sonder] Failed to move selected assets:", error);
                alert(error?.message || "Failed to move selected assets.");
            }
        });
    }

    async function handleBulkDelete(assetIds = selectedAssetIdsList()) {
        const ids = normalizeSelection(assetIds, state.selectedAssetId).ids;
        if (!ids.length) return;
        if (ids.length === 1) {
            const asset = data.assets.find((entry) => entry.asset_id === ids[0]);
            if (asset) {
                await handleAssetDelete(asset);
            }
            return;
        }
        if (!options.onBulkDeleteAssets) return;

        try {
            const message = `Move ${ids.length} selected asset(s) to Trash? You can restore them later until the trash is emptied.`;
            if (!confirm(message)) return;

            const result = await options.onBulkDeleteAssets(ids, false);
            if (result?.status === "conflict") {
                throw new Error("Bulk trash unexpectedly reported a conflict.");
            }

            for (const assetId of ids) {
                const asset = data.assets.find((entry) => entry.asset_id === assetId);
                if (!asset) continue;
                updateAsset({
                    ...asset,
                    folder: "",
                    trashed_at: result?.trashed_at || new Date().toISOString(),
                    trash_previous_folder: asset.trash_previous_folder || normalizeFolderName(asset.folder),
                });
            }
            clearUsageView();
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to trash selected assets:", error);
            alert(error?.message || "Failed to move selected assets to Trash.");
        }
    }

    async function handleAssetRestore(asset) {
        if (!asset?.asset_id || !options.onRestoreAsset) return;
        try {
            const result = await options.onRestoreAsset(asset.asset_id);
            updateAsset({
                ...asset,
                ...(result?.asset || {}),
                trashed_at: "",
                trash_previous_folder: "",
                folder: normalizeFolderName(result?.asset?.folder ?? asset.trash_previous_folder ?? ""),
            });
            clearUsageView();
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to restore asset:", error);
            alert(error?.message || "Failed to restore asset.");
        }
    }

    async function handleBulkRestore(assetIds = selectedAssetIdsList()) {
        const ids = normalizeSelection(assetIds, state.selectedAssetId).ids.filter((assetId) => isTrashed(data.assets.find((entry) => entry.asset_id === assetId)));
        if (!ids.length || !options.onBulkRestoreAssets) return;
        try {
            await options.onBulkRestoreAssets(ids);
            for (const assetId of ids) {
                const asset = data.assets.find((entry) => entry.asset_id === assetId);
                if (!asset) continue;
                updateAsset({
                    ...asset,
                    folder: normalizeFolderName(asset.trash_previous_folder || ""),
                    trashed_at: "",
                    trash_previous_folder: "",
                });
            }
            clearUsageView();
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to restore selected assets:", error);
            alert(error?.message || "Failed to restore selected assets.");
        }
    }

    async function getBulkUsagePayload(assetIds) {
        if (options.onGetBulkAssetUsages) {
            return await options.onGetBulkAssetUsages(assetIds) || { usages: [], usage_count: 0 };
        }
        if (options.onGetAssetUsages) {
            const payloads = await Promise.all(assetIds.map((assetId) => options.onGetAssetUsages(assetId)));
            return {
                usages: payloads.flatMap((payload) => payload?.usages || []),
                usage_count: payloads.reduce((sum, payload) => sum + (payload?.usage_count || 0), 0),
            };
        }
        return { usages: [], usage_count: 0 };
    }

    async function handleAssetPermanentDelete(asset) {
        if (!asset?.asset_id || !options.onPermanentDeleteAsset) return;
        try {
            const usage = await getBulkUsagePayload([asset.asset_id]);
            const counts = summarizeUsageTypes(usage?.usages || []);
            const message = usage?.usage_count > 0
                ? [
                    `Permanently delete "${assetDisplayName(asset)}"?`,
                    "This asset is still referenced.",
                    `Clips: ${counts.clip}`,
                    `Audio: ${counts.audio_track}`,
                    `Guides: ${counts.guide_frame}`,
                    `Queue: ${counts.generation_job}`,
                    "",
                    "Existing references will stay in place and become missing placeholders.",
                ].join("\n")
                : `Permanently delete "${assetDisplayName(asset)}"? This cannot be undone.`;
            if (!confirm(message)) return;

            const result = await options.onPermanentDeleteAsset(asset.asset_id, usage?.usage_count > 0);
            if (result?.status === "conflict") {
                throw new Error("Permanent delete unexpectedly reported a usage conflict.");
            }
            removeAssetsByIds([asset.asset_id]);
            clearUsageView();
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to permanently delete asset:", error);
            alert(error?.message || "Failed to permanently delete asset.");
        }
    }

    async function handleBulkPermanentDelete(assetIds = selectedAssetIdsList()) {
        const ids = normalizeSelection(assetIds, state.selectedAssetId).ids;
        if (!ids.length) return;
        if (ids.length === 1) {
            const asset = data.assets.find((entry) => entry.asset_id === ids[0]);
            if (asset) await handleAssetPermanentDelete(asset);
            return;
        }
        if (!options.onBulkPermanentDeleteAssets) return;

        try {
            const usage = await getBulkUsagePayload(ids);
            const counts = summarizeUsageTypes(usage?.usages || []);
            const message = usage?.usage_count > 0
                ? [
                    `Permanently delete ${ids.length} selected asset(s)?`,
                    "These assets are still referenced.",
                    `Clips: ${counts.clip}`,
                    `Audio: ${counts.audio_track}`,
                    `Guides: ${counts.guide_frame}`,
                    `Queue: ${counts.generation_job}`,
                    "",
                    "Existing references will stay in place and become missing placeholders.",
                ].join("\n")
                : `Permanently delete ${ids.length} selected asset(s)? This cannot be undone.`;
            if (!confirm(message)) return;

            const result = await options.onBulkPermanentDeleteAssets(ids, usage?.usage_count > 0);
            if (result?.status === "conflict") {
                throw new Error("Bulk permanent delete unexpectedly reported a usage conflict.");
            }
            removeAssetsByIds(ids);
            clearUsageView();
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to permanently delete selected assets:", error);
            alert(error?.message || "Failed to permanently delete selected assets.");
        }
    }

    async function handleEmptyTrash() {
        if (!options.onEmptyTrash) return;
        const ids = trashedAssets().map((asset) => asset.asset_id);
        if (!ids.length) return;
        if (!confirm(`Permanently delete all ${ids.length} asset(s) in Trash? This cannot be undone.`)) return;
        try {
            await options.onEmptyTrash();
            removeAssetsByIds(ids);
            clearUsageView();
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to empty trash:", error);
            alert(error?.message || "Failed to empty trash.");
        }
    }

    function renderBulkToolbar() {
        bulkToolbarHost.innerHTML = "";
        const assets = selectedAssets();
        if (assets.length <= 1) {
            bulkToolbarHost.style.display = "none";
            return;
        }

        bulkToolbarHost.style.display = "flex";
        const bar = style(document.createElement("div"), `display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px;border-radius:8px;background:rgba(75,105,135,0.14);border:1px solid rgba(111,142,168,0.4);flex-wrap:wrap;`);
        const label = style(document.createElement("div"), `color:#d9e7f3;font-size:10px;font-weight:700;`);
        label.textContent = `${assets.length} selected`;

        const actions = style(document.createElement("div"), `display:flex;flex-wrap:wrap;gap:6px;`);
        const activeAssets = assets.filter((asset) => !isTrashed(asset));
        const trashedSelection = assets.filter((asset) => isTrashed(asset));

        if (activeAssets.length) {
            const moveBtn = makeActionButton();
            moveBtn.textContent = activeAssets.length === assets.length ? "Move to..." : `Move ${activeAssets.length}`;
            moveBtn.disabled = !options.onBulkMoveAssets;
            moveBtn.addEventListener("click", async (event) => {
                await handleBulkMove(activeAssets.map((asset) => asset.asset_id), event);
            });
            actions.appendChild(moveBtn);

            const deleteBtn = makeActionButton();
            deleteBtn.textContent = activeAssets.length === assets.length ? "Trash" : `Trash ${activeAssets.length}`;
            deleteBtn.style.cssText = actionButtonCss("danger");
            deleteBtn.disabled = !options.onBulkDeleteAssets;
            deleteBtn.addEventListener("click", async () => {
                await handleBulkDelete(activeAssets.map((asset) => asset.asset_id));
            });
            actions.appendChild(deleteBtn);
        }

        if (trashedSelection.length) {
            const restoreBtn = makeActionButton();
            restoreBtn.textContent = trashedSelection.length === assets.length ? "Restore" : `Restore ${trashedSelection.length}`;
            restoreBtn.style.cssText = `padding:6px 10px;border-radius:6px;border:1px solid #48644f;background:#203427;color:#e5f7e8;cursor:pointer;font-size:11px;font-weight:600;box-shadow:inset 0 1px 0 rgba(255,255,255,0.04);`;
            restoreBtn.disabled = !options.onBulkRestoreAssets;
            restoreBtn.addEventListener("click", async () => {
                await handleBulkRestore(trashedSelection.map((asset) => asset.asset_id));
            });
            actions.appendChild(restoreBtn);

            const permanentBtn = makeActionButton();
            permanentBtn.textContent = trashedSelection.length === assets.length ? "Delete Permanently" : `Delete ${trashedSelection.length} Permanently`;
            permanentBtn.style.cssText = actionButtonCss("danger");
            permanentBtn.disabled = !options.onBulkPermanentDeleteAssets;
            permanentBtn.addEventListener("click", async () => {
                await handleBulkPermanentDelete(trashedSelection.map((asset) => asset.asset_id));
            });
            actions.appendChild(permanentBtn);
        }

        const clearBtn = makeActionButton();
        clearBtn.textContent = "Clear";
        clearBtn.style.cssText = actionButtonCss("subtle");
        clearBtn.addEventListener("click", () => {
            clearSelection();
        });
        actions.appendChild(clearBtn);

        bar.append(label, actions);
        bulkToolbarHost.appendChild(bar);
    }

    function renderPowerLoraRows(rows) {
        const entries = Array.isArray(rows) ? rows.filter((row) => row && typeof row === "object") : [];
        if (!entries.length) return null;
        const wrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:5px;min-width:0;`);
        const label = style(document.createElement("div"), `color:#8fa4b6;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;`);
        label.textContent = "Power LoRAs";
        wrap.appendChild(label);
        for (const row of entries) {
            const name = String(row.name || row.lora || row.label || "-");
            const token = fieldSearchToken("power_loras", name);
            const active = searchHasToken(token);
            const enabled = row.enabled !== false;
            const line = style(document.createElement("button"), `
                appearance:none;text-align:left;width:100%;min-width:0;
                display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;
                padding:7px 8px;border-radius:6px;border:1px solid ${active ? "rgba(143,192,240,0.58)" : CHROME.borderSoft};
                background:${active ? "rgba(143,192,240,0.16)" : "rgba(255,255,255,0.03)"};
                color:${enabled ? "#e4edf4" : "#9ca8b2"};cursor:pointer;
                box-shadow:${active ? "inset 0 0 0 1px rgba(143,192,240,0.22)" : "none"};
            `);
            const main = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:2px;`);
            const title = style(document.createElement("div"), `font-size:10px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
            title.textContent = `${row.slot != null ? `${row.slot}. ` : ""}${name}`;
            const parts = [];
            for (const [key, labelText] of [
                ["strength", "strength"],
                ["model_strength", "model"],
                ["clip_strength", "clip"],
            ]) {
                if (row[key] != null && row[key] !== "") parts.push(`${labelText} ${formatGenerationValue(row[key])}`);
            }
            const sub = style(document.createElement("div"), `color:#8fa4b6;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
            sub.textContent = parts.length ? parts.join(" | ") : "-";
            main.append(title, sub);
            const status = style(document.createElement("div"), `font-size:9px;font-weight:800;text-transform:uppercase;letter-spacing:0.05em;color:${enabled ? "#b7e4b4" : "#c0a49a"};`);
            status.textContent = enabled ? "On" : "Off";
            line.append(main, status);
            line.addEventListener("click", () => toggleSearchToken(token));
            wrap.appendChild(line);
        }
        return wrap;
    }

    function renderTrackedMetadataSection(asset) {
        const entries = trackedMetadataEntries(asset);
        if (!entries.length) return null;
        const wrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:8px;`);
        wrap.appendChild(makeSectionTitle("Tracked Metadata"));
        for (const entry of entries) {
            const section = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;padding:8px;border-radius:8px;background:rgba(255,255,255,0.025);border:1px solid ${CHROME.borderSoft};`);
            const title = style(document.createElement("div"), `color:#dce8f2;font-size:11px;font-weight:700;text-transform:uppercase;`);
            title.textContent = String(entry.label || "Tracked");
            section.appendChild(title);
            const sourceBits = [entry.source_node_class, entry.source_node_title].filter((part, index, all) => part && all.indexOf(part) === index);
            if (sourceBits.length) {
                const source = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                source.textContent = sourceBits.join(" | ");
                section.appendChild(source);
            }
            const fields = entry.fields && typeof entry.fields === "object" ? entry.fields : {};
            const powerLoras = renderPowerLoraRows(fields.power_loras);
            if (powerLoras) section.appendChild(powerLoras);
            const fieldEntries = Object.entries(fields).filter(([key]) => key !== "power_loras");
            if (fieldEntries.length) {
                const grid = style(document.createElement("div"), `display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;min-width:0;`);
                for (const [key, value] of fieldEntries) {
                    const rendered = formatGenerationValue(value);
                    const token = fieldSearchToken(key, rendered);
                    const active = searchHasToken(token);
                    const cell = makeMetaCell(key, rendered);
                    cell.style.cursor = "pointer";
                    if (active) {
                        cell.style.background = "rgba(143,192,240,0.16)";
                        cell.style.borderColor = "rgba(143,192,240,0.55)";
                        cell.style.boxShadow = "inset 0 0 0 1px rgba(143,192,240,0.22)";
                    }
                    cell.addEventListener("click", () => {
                        toggleSearchToken(token);
                    });
                    grid.appendChild(cell);
                }
                section.appendChild(grid);
            }
            if (entry.raw_widget_text) {
                const details = document.createElement("details");
                details.style.cssText = `font-size:10px;color:#cfd8df;`;
                const summary = document.createElement("summary");
                summary.textContent = "Raw";
                summary.style.cursor = "pointer";
                const raw = style(document.createElement("pre"), `margin:6px 0 0 0;white-space:pre-wrap;word-break:break-word;color:#d9e0e6;background:rgba(0,0,0,0.18);border-radius:6px;padding:6px;`);
                raw.textContent = String(entry.raw_widget_text || "");
                details.append(summary, raw);
                section.appendChild(details);
            }
            wrap.appendChild(section);
        }
        return wrap;
    }

    function renderGenerationSection(asset) {
        const wrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;`);
        if (asset.prompt) {
            wrap.appendChild(makeSectionTitle("Prompt"));
            const promptBox = style(document.createElement("div"), `padding:8px;border-radius:8px;background:rgba(255,255,255,0.03);border:1px solid ${CHROME.borderSoft};color:#d9e0e6;font-size:10px;line-height:1.45;white-space:pre-wrap;`);
            promptBox.textContent = asset.prompt;
            wrap.appendChild(promptBox);
        }
        const generationEntries = Object.entries(asset.generation_params || {});
        if (generationEntries.length) {
            wrap.appendChild(makeSectionTitle("Generation"));
            const grid = style(document.createElement("div"), `display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;min-width:0;`);
            for (const [key, value] of generationEntries) {
                grid.appendChild(makeMetaCell(key, formatGenerationValue(value)));
            }
            wrap.appendChild(grid);
        }
        if (!asset.prompt && !generationEntries.length) {
            const empty = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;font-style:italic;`);
            empty.textContent = "No generation metadata stored for this asset.";
            wrap.appendChild(empty);
        }
        return wrap;
    }

    function renderOverlayMetadataPanel(assets) {
        const assetList = (Array.isArray(assets) ? assets : [assets]).filter(Boolean);
        const panel = style(document.createElement("div"), `flex:0 0 min(360px,32vw);max-width:420px;min-width:260px;min-height:0;overflow:auto;display:flex;flex-direction:column;gap:10px;padding:10px;border-radius:10px;background:rgba(10,15,20,0.72);border:1px solid ${CHROME.border};`);
        const header = style(document.createElement("div"), `color:#f1f5f8;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;`);
        header.textContent = assetList.length > 1 ? "Compare Metadata" : "Metadata";
        panel.appendChild(header);
        for (const item of assetList) {
            const section = style(document.createElement("div"), `display:flex;flex-direction:column;gap:8px;min-width:0;`);
            if (assetList.length > 1) {
                const title = style(document.createElement("div"), `color:#dce8f2;font-size:11px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                title.textContent = assetDisplayName(item);
                title.title = assetDisplayName(item);
                section.appendChild(title);
            }
            const quick = style(document.createElement("div"), `display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;`);
            quick.append(
                makeMetaCell("Type", assetKindLabel(item.asset_type)),
                makeMetaCell("Workflow", workflowStatusLabel(item)),
                makeMetaCell("Duration", formatDuration(item)),
                makeMetaCell("Size", item.asset_type === "artifact" ? formatBytes(item.size_bytes) : formatResolution(item)),
            );
            section.appendChild(quick);
            const tracked = renderTrackedMetadataSection(item);
            if (tracked) section.appendChild(tracked);
            section.appendChild(renderGenerationSection(item));
            panel.appendChild(section);
        }
        return panel;
    }

    function renderMediaScrubBar(mediaEl) {
        const wrap = style(document.createElement("div"), `display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;background:rgba(255,255,255,0.03);border:1px solid ${CHROME.borderSoft};`);
        const track = style(document.createElement("div"), `position:relative;flex:1 1 auto;height:10px;border-radius:999px;background:#1a2631;cursor:pointer;overflow:hidden;`);
        const fill = style(document.createElement("div"), `position:absolute;left:0;top:0;bottom:0;width:0;background:linear-gradient(90deg,#6fa7d8,#8fc0f0);`);
        const thumb = style(document.createElement("div"), `position:absolute;top:50%;width:12px;height:12px;border-radius:50%;background:#d9ebfb;border:1px solid rgba(0,0,0,0.35);transform:translate(-50%,-50%);left:100%;pointer-events:none;box-shadow:0 1px 3px rgba(0,0,0,0.35);`);
        fill.appendChild(thumb);
        track.appendChild(fill);
        const label = style(document.createElement("div"), `color:#a9bccb;font-size:10px;white-space:nowrap;min-width:72px;text-align:right;`);
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
        const handlePointerDown = (event) => {
            event.preventDefault();
            dragging = true;
            seekFromClientX(event.clientX);
            window.addEventListener("mousemove", handlePointerMove);
            window.addEventListener("mouseup", handlePointerUp);
        };

        track.addEventListener("mousedown", handlePointerDown);
        mediaEl?.addEventListener?.("timeupdate", updateUI);
        mediaEl?.addEventListener?.("loadedmetadata", updateUI);
        mediaEl?.addEventListener?.("durationchange", updateUI);
        mediaEl?.addEventListener?.("ended", updateUI);
        updateUI();

        return {
            el: wrap,
            cleanup() {
                dragging = false;
                window.removeEventListener("mousemove", handlePointerMove);
                window.removeEventListener("mouseup", handlePointerUp);
                track.removeEventListener("mousedown", handlePointerDown);
                mediaEl?.removeEventListener?.("timeupdate", updateUI);
                mediaEl?.removeEventListener?.("loadedmetadata", updateUI);
                mediaEl?.removeEventListener?.("durationchange", updateUI);
                mediaEl?.removeEventListener?.("ended", updateUI);
            },
        };
    }

    function renderSynchronizedScrubBar(mediaEls, opts = {}) {
        const mediaList = (mediaEls || []).filter(Boolean);
        const { transport = null } = opts;
        const wrap = style(document.createElement("div"), `display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:6px;background:rgba(255,255,255,0.03);border:1px solid #343434;`);
        const track = style(document.createElement("div"), `position:relative;flex:1 1 auto;height:10px;border-radius:999px;background:#1a2631;cursor:pointer;overflow:hidden;`);
        const fill = style(document.createElement("div"), `position:absolute;left:0;top:0;bottom:0;width:0;background:linear-gradient(90deg,#6fa7d8,#8fc0f0);`);
        track.appendChild(fill);
        const label = style(document.createElement("div"), `color:#a9bccb;font-size:10px;white-space:nowrap;min-width:72px;text-align:right;`);
        wrap.append(track, label);

        let dragging = false;
        const primary = () => mediaList[0] || null;
        const duration = () => {
            if (transport) return transport.duration();
            const values = mediaList
                .map((media) => Number(media?.duration))
                .filter((value) => Number.isFinite(value) && value > 0);
            return values.length ? Math.max(...values) : 0;
        };
        const currentTime = () => {
            if (transport) return clamp(transport.getPlayhead(), 0, duration() || Number.MAX_SAFE_INTEGER);
            return clamp(Number(primary()?.currentTime) || 0, 0, duration() || Number.MAX_SAFE_INTEGER);
        };
        const updateUI = () => {
            const total = duration();
            const current = currentTime();
            const ratio = total > 0 ? current / total : 0;
            fill.style.width = `${ratio * 100}%`;
            label.textContent = `${formatClockTime(current)} / ${formatClockTime(total)}`;
        };
        const seekFromClientX = (clientX) => {
            const total = duration();
            if (!total) return;
            const rect = track.getBoundingClientRect();
            const ratio = clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1);
            const nextTime = ratio * total;
            if (transport) {
                transport.seek(nextTime);
            } else {
                for (const media of mediaList) {
                    media.currentTime = nextTime;
                }
            }
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
        const handlePointerDown = (event) => {
            event.preventDefault();
            dragging = true;
            seekFromClientX(event.clientX);
            window.addEventListener("mousemove", handlePointerMove);
            window.addEventListener("mouseup", handlePointerUp);
        };

        track.addEventListener("mousedown", handlePointerDown);
        const unsubscribeTransport = transport?.subscribe?.(updateUI) || null;
        for (const media of mediaList) {
            media.addEventListener?.("timeupdate", updateUI);
            media.addEventListener?.("loadedmetadata", updateUI);
            media.addEventListener?.("durationchange", updateUI);
            media.addEventListener?.("ended", updateUI);
        }
        updateUI();

        return {
            el: wrap,
            cleanup() {
                dragging = false;
                window.removeEventListener("mousemove", handlePointerMove);
                window.removeEventListener("mouseup", handlePointerUp);
                track.removeEventListener("mousedown", handlePointerDown);
                unsubscribeTransport?.();
                for (const media of mediaList) {
                    media.removeEventListener?.("timeupdate", updateUI);
                    media.removeEventListener?.("loadedmetadata", updateUI);
                    media.removeEventListener?.("durationchange", updateUI);
                    media.removeEventListener?.("ended", updateUI);
                }
            },
        };
    }

    function mediaDurationSeconds(media, fallback = 0) {
        const value = Number(media?.duration);
        if (Number.isFinite(value) && value > 0) return value;
        const safeFallback = Number(fallback);
        return Number.isFinite(safeFallback) && safeFallback > 0 ? safeFallback : 0;
    }

    function assetDurationSeconds(asset) {
        const value = Number(asset?.duration_sec);
        if (Number.isFinite(value) && value > 0) return value;
        const frames = Number(asset?.frame_count);
        const fps = assetFps(asset);
        return Number.isFinite(frames) && frames > 0 ? frames / fps : 0;
    }

    function assetFps(asset) {
        const candidates = [
            asset?.metadata?.fps,
            asset?.generation_params?.fps,
            asset?.fps,
        ];
        for (const value of candidates) {
            const fps = Number(value);
            if (Number.isFinite(fps) && fps > 0) return fps;
        }
        return 30;
    }

    function createLinkedMediaTransport(mediaEls, options = {}) {
        const mediaList = (mediaEls || []).filter(Boolean);
        const fallbackDurations = Array.isArray(options.fallbackDurations) ? options.fallbackDurations : [];
        const listeners = new Set();
        let playhead = 0;
        let playing = false;
        let rafId = 0;
        let lastDriftCorrectionAt = 0;

        const duration = () => {
            const values = mediaList.map((media, index) => mediaDurationSeconds(media, fallbackDurations[index] || 0));
            return values.length ? Math.max(...values) : 0;
        };
        const snapshot = () => ({ playhead, duration: duration(), playing });
        const notify = () => {
            const value = snapshot();
            for (const listener of listeners) {
                listener(value);
            }
        };
        const mediaTargetTime = (media, index, nextTime) => {
            const mediaDuration = mediaDurationSeconds(media, fallbackDurations[index] || duration());
            return mediaDuration > 0 ? clamp(nextTime, 0, mediaDuration) : Math.max(0, nextTime);
        };
        const primaryReferenceTime = () => {
            const primary = mediaList[0];
            const primaryTime = Number(primary?.currentTime);
            if (!Number.isFinite(primaryTime)) return null;
            const total = duration();
            const primaryDuration = mediaDurationSeconds(primary, fallbackDurations[0] || total);
            if (primaryDuration > 0 && total > primaryDuration + 0.05 && primaryTime >= primaryDuration - 0.05) {
                return null;
            }
            return primaryTime;
        };
        const playbackClockTime = () => {
            const primaryTime = primaryReferenceTime();
            if (Number.isFinite(primaryTime)) return primaryTime;
            const values = mediaList
                .map((media) => Number(media?.currentTime))
                .filter((value) => Number.isFinite(value));
            return values.length ? Math.max(...values) : playhead;
        };
        const syncMediaTimes = (force = false) => {
            mediaList.forEach((media, index) => {
                const target = mediaTargetTime(media, index, playhead);
                if (force || Math.abs((Number(media.currentTime) || 0) - target) > 0.04) {
                    try {
                        media.currentTime = target;
                    } catch {
                        // Some browsers reject currentTime before metadata is loaded.
                    }
                }
            });
        };
        const recoverSecondaryDrift = (timestamp) => {
            if (!playing || mediaList.length < 2) return;
            if (timestamp - lastDriftCorrectionAt < 1000) return;
            const primaryTime = primaryReferenceTime();
            if (!Number.isFinite(primaryTime)) return;
            for (let index = 1; index < mediaList.length; index++) {
                const media = mediaList[index];
                if (!media || media.seeking || media.readyState < 2) continue;
                const target = mediaTargetTime(media, index, primaryTime);
                const drift = Math.abs((Number(media.currentTime) || 0) - target);
                if (drift < 0.25) continue;
                try {
                    media.currentTime = target;
                    lastDriftCorrectionAt = timestamp;
                } catch {
                    // Some browsers reject currentTime before metadata is loaded.
                }
            }
        };
        const stopRaf = () => {
            if (rafId) {
                cancelAnimationFrame(rafId);
                rafId = 0;
            }
        };
        const pause = () => {
            const wasPlaying = playing;
            playing = false;
            stopRaf();
            for (const media of mediaList) {
                media.pause?.();
            }
            if (wasPlaying) notify();
        };
        const tick = (timestamp) => {
            if (!playing) return;
            const total = duration();
            playhead = clamp(playbackClockTime(), 0, total || Number.MAX_SAFE_INTEGER);
            recoverSecondaryDrift(timestamp);
            notify();
            if (total > 0 && playhead >= total - 0.001) {
                pause();
                return;
            }
            rafId = requestAnimationFrame(tick);
        };
        const play = () => {
            const total = duration();
            if (total > 0 && playhead >= total - 0.001) {
                playhead = 0;
            }
            syncMediaTimes(true);
            playing = true;
            for (const [index, media] of mediaList.entries()) {
                const mediaDuration = mediaDurationSeconds(media, fallbackDurations[index] || total);
                if (mediaDuration > 0 && playhead >= mediaDuration - 0.001) continue;
                void media.play?.().catch?.(() => {});
            }
            stopRaf();
            rafId = requestAnimationFrame(tick);
            notify();
        };
        const seek = (nextTime) => {
            playhead = clamp(Number(nextTime) || 0, 0, duration() || Number.MAX_SAFE_INTEGER);
            syncMediaTimes(true);
            notify();
        };
        const step = (deltaSeconds) => {
            pause();
            seek(playhead + deltaSeconds);
        };

        const metadataHandler = () => {
            if (!playing) syncMediaTimes(false);
            notify();
        };
        for (const media of mediaList) {
            media.addEventListener?.("loadedmetadata", metadataHandler);
            media.addEventListener?.("durationchange", metadataHandler);
        }

        return {
            duration,
            getPlayhead: () => playhead,
            isPlaying: () => playing,
            play,
            pause,
            toggle: () => (playing ? pause() : play()),
            seek,
            step,
            subscribe(listener) {
                listeners.add(listener);
                listener(snapshot());
                return () => listeners.delete(listener);
            },
            cleanup() {
                pause();
                stopRaf();
                listeners.clear();
                for (const media of mediaList) {
                    media.removeEventListener?.("loadedmetadata", metadataHandler);
                    media.removeEventListener?.("durationchange", metadataHandler);
                }
            },
        };
    }

    function attachRightClickVideoScrub(surface, transport, mediaEls, options = {}) {
        if (!surface || !transport) return () => {};
        const mediaList = (mediaEls || []).filter(Boolean);
        const readout = style(document.createElement("div"), `position:fixed;z-index:100002;padding:4px 7px;border-radius:6px;background:rgba(5,10,15,0.92);border:1px solid ${CHROME.borderStrong};color:${CHROME.text};font-size:10px;pointer-events:none;opacity:0;transition:opacity 140ms ease;`);
        document.body.appendChild(readout);
        let pending = false;
        let dragging = false;
        let startX = 0;
        let startY = 0;
        let startTime = 0;
        let previousMuted = [];

        const setReadout = (event, nextTime) => {
            const fps = Number(options.fps) || 30;
            const frame = Math.max(0, Math.round(nextTime * fps));
            readout.textContent = `${formatClockTime(nextTime)} | f${frame}`;
            readout.style.left = `${event.clientX + 12}px`;
            readout.style.top = `${event.clientY + 12}px`;
            readout.style.opacity = "1";
        };
        const timeFromDeltaX = (clientX) => {
            const total = transport.duration();
            if (!total) return 0;
            const rect = surface.getBoundingClientRect();
            const deltaRatio = (clientX - startX) / Math.max(1, rect.width);
            return clamp(startTime + deltaRatio * total, 0, total);
        };
        const beginMute = () => {
            previousMuted = mediaList.map((media) => media.muted);
            for (const media of mediaList) {
                media.muted = true;
            }
        };
        const endMute = () => {
            mediaList.forEach((media, index) => {
                media.muted = previousMuted[index] ?? media.muted;
            });
            previousMuted = [];
        };
        const scrubToEvent = (event) => {
            const nextTime = timeFromDeltaX(event.clientX);
            transport.seek(nextTime);
            setReadout(event, nextTime);
        };
        const handleMouseMove = (event) => {
            if (!pending) return;
            if (!dragging) {
                const distance = Math.hypot(event.clientX - startX, event.clientY - startY);
                if (distance < 5) return;
                dragging = true;
            }
            event.preventDefault();
            scrubToEvent(event);
        };
        const handleMouseUp = (event) => {
            if (!pending) return;
            if (dragging) {
                scrubToEvent(event);
            }
            pending = false;
            dragging = false;
            endMute();
            window.removeEventListener("mousemove", handleMouseMove);
            window.removeEventListener("mouseup", handleMouseUp);
            setTimeout(() => {
                readout.style.opacity = "0";
            }, 180);
        };
        const handleMouseDown = (event) => {
            if (event.button !== 2) return;
            event.preventDefault();
            event.stopPropagation();
            pending = true;
            dragging = false;
            startX = event.clientX;
            startY = event.clientY;
            startTime = transport.getPlayhead();
            beginMute();
            window.addEventListener("mousemove", handleMouseMove);
            window.addEventListener("mouseup", handleMouseUp);
        };
        const handleContextMenu = (event) => {
            event.preventDefault();
        };

        surface.addEventListener("mousedown", handleMouseDown);
        surface.addEventListener("contextmenu", handleContextMenu);

        return () => {
            pending = false;
            dragging = false;
            endMute();
            window.removeEventListener("mousemove", handleMouseMove);
            window.removeEventListener("mouseup", handleMouseUp);
            surface.removeEventListener("mousedown", handleMouseDown);
            surface.removeEventListener("contextmenu", handleContextMenu);
            readout.remove();
        };
    }

    function showInspectOverlayShortcutHelp() {
        const overlayEl = state.overlayState.overlayEl;
        if (!overlayEl) return;
        const existing = overlayEl.querySelector("[data-sonder-inspect-help='1']");
        if (existing) {
            existing.remove();
            return;
        }
        const backdrop = style(document.createElement("div"), `position:fixed;inset:0;z-index:100001;background:rgba(0,0,0,0.46);display:flex;align-items:center;justify-content:center;padding:24px;box-sizing:border-box;`);
        backdrop.dataset.sonderInspectHelp = "1";
        const panel = style(document.createElement("div"), `width:min(520px,100%);max-height:min(680px,90vh);overflow:auto;border-radius:12px;background:${CHROME.panelRaised};border:1px solid ${CHROME.borderStrong};box-shadow:0 18px 60px rgba(0,0,0,0.48);padding:16px;box-sizing:border-box;`);
        const header = style(document.createElement("div"), `display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;`);
        const title = style(document.createElement("div"), `color:${CHROME.text};font-size:13px;font-weight:700;`);
        title.textContent = "Inspect Overlay Shortcuts";
        const closeBtn = makeActionButton("subtle");
        closeBtn.textContent = "Close";
        header.append(title, closeBtn);
        const list = style(document.createElement("div"), `display:flex;flex-direction:column;gap:4px;`);
        for (const [key, desc] of INSPECT_OVERLAY_SHORTCUTS) {
            const row = style(document.createElement("div"), `display:grid;grid-template-columns:minmax(116px,auto) minmax(0,1fr);gap:12px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,0.05);`);
            const keyEl = style(document.createElement("div"), `color:#9fc9ec;font-size:11px;font-family:monospace;white-space:nowrap;`);
            keyEl.textContent = key;
            const descEl = style(document.createElement("div"), `color:${CHROME.text};font-size:11px;min-width:0;`);
            descEl.textContent = desc;
            row.append(keyEl, descEl);
            list.appendChild(row);
        }
        panel.append(header, list);
        backdrop.appendChild(panel);
        overlayEl.appendChild(backdrop);
        const close = () => backdrop.remove();
        closeBtn.addEventListener("click", close);
        backdrop.addEventListener("mousedown", (event) => {
            if (event.target === backdrop) close();
        });
        state.overlayState.cleanupFns.push(() => backdrop.remove());
    }

    function clearOverlayRuntime() {
        const overlay = state.overlayState;
        for (const cleanup of overlay.cleanupFns.splice(0)) {
            try {
                cleanup?.();
            } catch (error) {
                console.warn("[Sonder] Overlay cleanup failed:", error);
            }
        }
        overlay.togglePlayback = null;
        overlay.stepVideoCompare = null;
        overlay.videoCompareFps = 30;
        overlay.applyAudioMonitor = null;
    }

    function overlayAssets() {
        return navigableAssets();
    }

    function currentOverlayAsset() {
        const assetId = state.overlayState.assetId;
        if (!assetId) return null;
        const fromVisible = overlayAssets().find((asset) => asset.asset_id === assetId);
        if (fromVisible) return fromVisible;
        return filteredAssets().find((asset) => asset.asset_id === assetId) || null;
    }

    function sameTypeOverlayAssets(asset) {
        return asset ? overlayAssets().filter((entry) => entry.asset_type === asset.asset_type) : [];
    }

    function resetOverlayTransform() {
        state.overlayState.zoomLevel = 1;
        state.overlayState.panX = 0;
        state.overlayState.panY = 0;
        state.overlayState.sideBySideTransforms = {
            a: { zoomLevel: 1, panX: 0, panY: 0 },
            b: { zoomLevel: 1, panX: 0, panY: 0 },
        };
    }

    function closeInspectOverlay() {
        if (!state.overlayState.open) return;
        clearOverlayRuntime();
        state.overlayState.open = false;
        state.overlayState.assetId = "";
        state.overlayState.compareMode = false;
        state.overlayState.showMetadata = false;
        state.overlayState.compareLeftAssetId = "";
        state.overlayState.compareRightAssetId = "";
        state.overlayState.comparePickerQuery = "";
        state.overlayState.comparePickerSortMode = DEFAULT_SORT_MODE;
        state.overlayState.showWaveform = false;
        state.overlayState.audioFocus = "none";
        state.overlayState.audioTempFlip = false;
        state.overlayState.togglePlayback = null;
        state.overlayState.stepVideoCompare = null;
        state.overlayState.videoCompareFps = 30;
        state.overlayState.applyAudioMonitor = null;
        state.overlayState.overlayEl?.remove();
        state.overlayState.overlayEl = null;
    }

    function attachZoomPan(surface, targets, options = {}) {
        const normalizedOptions = typeof options === "function"
            ? { onTransform: options }
            : (options && typeof options === "object" ? options : {});
        const { onTransform = null, onClick = null, transformState = state.overlayState } = normalizedOptions;
        const targetList = (Array.isArray(targets) ? targets : [targets]).filter(Boolean);
        let dragging = false;
        let pendingDrag = false;
        let suppressClick = false;
        let lastX = 0;
        let lastY = 0;
        let dragStartX = 0;
        let dragStartY = 0;
        let lastDragMouseUpAt = 0;

        const applyTransform = () => {
            const transform = `translate(${transformState.panX}px, ${transformState.panY}px) scale(${transformState.zoomLevel})`;
            for (const target of targetList) {
                target.style.transformOrigin = "center center";
                target.style.transform = transform;
            }
            onTransform?.();
        };

        const updateCursor = () => {
            surface.style.cursor = pendingDrag || dragging ? "grabbing" : "grab";
        };

        const handleWheel = (event) => {
            event.preventDefault();
            const rect = surface.getBoundingClientRect();
            const previousZoom = transformState.zoomLevel;
            const zoomingIn = event.deltaY < 0;
            const nextZoom = clamp(previousZoom * (zoomingIn ? 1.12 : (1 / 1.12)), 1, 16);
            if (nextZoom === previousZoom) return;
            if (zoomingIn) {
                const cursorX = event.clientX - rect.left - rect.width / 2;
                const cursorY = event.clientY - rect.top - rect.height / 2;
                const scale = nextZoom / previousZoom;
                transformState.panX -= cursorX * (scale - 1);
                transformState.panY -= cursorY * (scale - 1);
            } else {
                const denom = Math.max(0.0001, previousZoom - 1);
                const factor = Math.max(0, (nextZoom - 1) / denom);
                transformState.panX *= factor;
                transformState.panY *= factor;
            }
            transformState.zoomLevel = nextZoom;
            applyTransform();
        };

        const handleMouseMove = (event) => {
            if (!pendingDrag && !dragging) return;
            if (!dragging) {
                const distance = Math.hypot(event.clientX - dragStartX, event.clientY - dragStartY);
                if (distance < 5) return;
                dragging = true;
                lastX = event.clientX;
                lastY = event.clientY;
                event.preventDefault();
            }
            transformState.panX += event.clientX - lastX;
            transformState.panY += event.clientY - lastY;
            lastX = event.clientX;
            lastY = event.clientY;
            applyTransform();
        };

        const handleMouseUp = (event) => {
            const dragged = dragging;
            if (dragged) lastDragMouseUpAt = performance.now();
            dragging = false;
            pendingDrag = false;
            updateCursor();
            window.removeEventListener("mousemove", handleMouseMove);
            window.removeEventListener("mouseup", handleMouseUp);
            if (dragged || (!dragged && onClick)) {
                suppressClick = true;
                setTimeout(() => {
                    suppressClick = false;
                }, 0);
            }
            if (!dragged && onClick && (!event?.target || surface.contains(event.target))) {
                onClick(event);
            }
        };

        const handleMouseDown = (event) => {
            if (event.button !== 0) return;
            event.preventDefault();
            pendingDrag = true;
            dragging = false;
            dragStartX = event.clientX;
            dragStartY = event.clientY;
            lastX = event.clientX;
            lastY = event.clientY;
            updateCursor();
            window.addEventListener("mousemove", handleMouseMove);
            window.addEventListener("mouseup", handleMouseUp);
        };

        const handleDragStart = (event) => {
            event.preventDefault();
        };

        const handleClickCapture = (event) => {
            if (!suppressClick) return;
            suppressClick = false;
            event.preventDefault();
            event.stopPropagation();
            event.stopImmediatePropagation?.();
        };

        const handleDoubleClick = () => {
            if (performance.now() - lastDragMouseUpAt < 350) return;
            transformState.zoomLevel = 1;
            transformState.panX = 0;
            transformState.panY = 0;
            applyTransform();
        };

        surface.addEventListener("wheel", handleWheel, { passive: false });
        surface.addEventListener("mousedown", handleMouseDown);
        surface.addEventListener("click", handleClickCapture, true);
        surface.addEventListener("dblclick", handleDoubleClick);
        surface.addEventListener("dragstart", handleDragStart);
        updateCursor();
        applyTransform();

        return () => {
            dragging = false;
            pendingDrag = false;
            suppressClick = false;
            surface.removeEventListener("wheel", handleWheel);
            surface.removeEventListener("mousedown", handleMouseDown);
            surface.removeEventListener("click", handleClickCapture, true);
            surface.removeEventListener("dblclick", handleDoubleClick);
            surface.removeEventListener("dragstart", handleDragStart);
            window.removeEventListener("mousemove", handleMouseMove);
            window.removeEventListener("mouseup", handleMouseUp);
            surface.style.cursor = "";
        };
    }

    function renderWaveformPanel(assets, colors) {
        const assetList = (Array.isArray(assets) ? assets : [assets]).filter(Boolean);
        const wrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;padding:8px;border-radius:8px;background:rgba(255,255,255,0.03);border:1px solid #343434;`);
        const label = style(document.createElement("div"), `color:#a9bccb;font-size:10px;text-transform:uppercase;letter-spacing:0.06em;`);
        label.textContent = assetList.length > 1 ? "Waveform Compare" : "Waveform";
        const status = style(document.createElement("div"), `color:#8ea0af;font-size:10px;`);
        status.textContent = "Loading waveform...";
        const canvas = style(document.createElement("canvas"), `width:100%;height:88px;border-radius:6px;background:#0a0f13;display:block;`);
        wrap.append(label, status, canvas);

        let active = true;

        const draw = (datasets) => {
            if (!active) return;
            const rect = canvas.getBoundingClientRect();
            const width = Math.max(320, Math.floor(rect.width || 640));
            const height = 88;
            canvas.width = width;
            canvas.height = height;
            const ctx = canvas.getContext("2d");
            ctx.clearRect(0, 0, width, height);
            ctx.fillStyle = "#0a0f13";
            ctx.fillRect(0, 0, width, height);
            ctx.strokeStyle = "rgba(255,255,255,0.08)";
            ctx.beginPath();
            ctx.moveTo(0, height / 2);
            ctx.lineTo(width, height / 2);
            ctx.stroke();

            datasets.forEach((dataset, datasetIndex) => {
                if (!dataset?.peaks?.length) return;
                ctx.strokeStyle = colors?.[datasetIndex] || "#7fc0ff";
                ctx.globalAlpha = datasetIndex === 0 ? 0.9 : 0.7;
                const peaks = dataset.peaks;
                const step = width / peaks.length;
                for (let i = 0; i < peaks.length; i++) {
                    const [minValue, maxValue] = peaks[i];
                    const x = i * step;
                    const y1 = clamp((1 - ((maxValue + 1) / 2)) * height, 0, height);
                    const y2 = clamp((1 - ((minValue + 1) / 2)) * height, 0, height);
                    ctx.beginPath();
                    ctx.moveTo(x, y1);
                    ctx.lineTo(x, y2);
                    ctx.stroke();
                }
            });
            ctx.globalAlpha = 1;
        };

        Promise.all(assetList.map(async (asset) => {
            try {
                const resp = await fetch(buildWaveformUrl(currentProjectDir(), asset.asset_id));
                return resp.ok ? await resp.json() : null;
            } catch {
                return null;
            }
        })).then((datasets) => {
            if (!active) return;
            if (!datasets.some((dataset) => dataset?.peaks?.length)) {
                status.textContent = "Waveform unavailable.";
                return;
            }
            status.textContent = assetList.length > 1
                ? assetList.map((asset, index) => `${assetDisplayName(asset)}: ${colors?.[index] || "#7fc0ff"}`).join(" | ")
                : assetDisplayName(assetList[0]);
            requestAnimationFrame(() => draw(datasets));
        });

        return {
            el: wrap,
            cleanup() {
                active = false;
            },
        };
    }

    function renderInteractiveWaveform(assets, mediaEls, colors, opts = {}) {
        const { enableZoom = false, compact = false, layout = "overlay", transport = null } = opts;
        const assetList = (Array.isArray(assets) ? assets : [assets]).filter(Boolean);
        const mediaList = (Array.isArray(mediaEls) ? mediaEls : [mediaEls]).filter(Boolean);
        const primary = () => mediaList[0] || null;
        const stackedLayout = assetList.length > 1 && layout === "stacked";

        const wrapStyle = compact
            ? `display:flex;flex-direction:column;gap:6px;padding:8px;border-radius:8px;background:rgba(255,255,255,0.03);border:1px solid #343434;`
            : `display:flex;flex-direction:column;gap:6px;padding:8px;border-radius:8px;background:rgba(255,255,255,0.03);border:1px solid #343434;flex:1 1 auto;min-height:0;`;
        const wrap = style(document.createElement("div"), wrapStyle);
        const headerRow = style(document.createElement("div"), `display:flex;align-items:center;justify-content:space-between;gap:8px;flex:0 0 auto;`);
        const status = style(document.createElement("div"), `color:#8ea0af;font-size:10px;flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        status.textContent = "Loading waveform...";
        const timeLabel = style(document.createElement("div"), `color:#a9bccb;font-size:10px;white-space:nowrap;`);
        headerRow.append(status, timeLabel);
        const canvasStyle = compact
            ? `width:100%;height:88px;border-radius:6px;background:#0a0f13;display:block;cursor:pointer;`
            : `width:100%;flex:1 1 0;min-height:${stackedLayout ? 180 : 120}px;height:100%;border-radius:6px;background:#0a0f13;display:block;cursor:pointer;`;
        const canvas = style(document.createElement("canvas"), canvasStyle);
        wrap.append(headerRow, canvas);

        let datasets = null;
        let active = true;
        let dragging = false;
        let zoomLevel = 1;
        let viewOffset = 0;

        const duration = () => {
            if (transport) return transport.duration();
            const values = mediaList.map((m) => Number(m?.duration)).filter((v) => Number.isFinite(v) && v > 0);
            if (values.length) return Math.max(...values);
            const assetValues = assetList.map(assetDurationSeconds).filter((v) => Number.isFinite(v) && v > 0);
            return assetValues.length ? Math.max(...assetValues) : 0;
        };
        const currentTime = () => {
            if (transport) return clamp(transport.getPlayhead(), 0, duration() || Number.MAX_SAFE_INTEGER);
            return clamp(Number(primary()?.currentTime) || 0, 0, duration() || Number.MAX_SAFE_INTEGER);
        };
        const updateTimeLabel = () => {
            if (assetList.length > 1) {
                const parts = assetList.slice(0, 2).map((asset, index) => {
                    const letter = index === 0 ? "A" : "B";
                    const seconds = assetDurationSeconds(asset) || mediaDurationSeconds(mediaList[index], 0);
                    return `${letter}: ${seconds ? seconds.toFixed(1) : "0.0"}s`;
                });
                timeLabel.textContent = `${formatClockTime(currentTime())} / ${formatClockTime(duration())} (${parts.join(" | ")})`;
                return;
            }
            timeLabel.textContent = `${formatClockTime(currentTime())} / ${formatClockTime(duration())}`;
        };

        const draw = () => {
            if (!active) return;
            const rect = canvas.getBoundingClientRect();
            const dpr = window.devicePixelRatio || 1;
            const width = Math.max(320, Math.floor(rect.width || 640));
            const height = compact ? 88 : Math.max(stackedLayout ? 180 : 120, Math.floor(rect.height || 88));
            canvas.width = Math.floor(width * dpr);
            canvas.height = Math.floor(height * dpr);
            const ctx = canvas.getContext("2d");
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, width, height);
            ctx.fillStyle = "#0a0f13";
            ctx.fillRect(0, 0, width, height);
            ctx.strokeStyle = "rgba(255,255,255,0.08)";
            ctx.beginPath();
            ctx.moveTo(0, height / 2);
            ctx.lineTo(width, height / 2);
            ctx.stroke();

            if (!datasets || !datasets.some((d) => d?.peaks?.length)) {
                updateTimeLabel();
                return;
            }

            const visibleRange = 1 / zoomLevel;
            const viewStart = viewOffset;
            const viewEnd = viewOffset + visibleRange;
            const total = duration();
            const progressRatio = total > 0 ? currentTime() / total : 0;
            const progressCanvasX = total > 0 ? ((progressRatio - viewStart) / visibleRange) * width : 0;

            const rowForDataset = (datasetIndex) => {
                if (!stackedLayout) return { y: 0, height, label: "" };
                const gap = 8;
                const rowCount = Math.max(1, datasets.length);
                const rowHeight = Math.max(44, (height - gap * (rowCount - 1)) / rowCount);
                return {
                    y: datasetIndex * (rowHeight + gap),
                    height: rowHeight,
                    label: `${datasetIndex === 0 ? "A" : "B"} ${assetDisplayName(assetList[datasetIndex])}`,
                };
            };
            const drawDataset = (dataset, datasetIndex, row, bright, clipStartX = 0, clipEndX = width) => {
                if (!dataset?.peaks?.length || !total) return;
                ctx.save();
                if (clipStartX !== 0 || clipEndX !== width) {
                    ctx.beginPath();
                    ctx.rect(clipStartX, row.y, clipEndX - clipStartX, row.height);
                    ctx.clip();
                }
                ctx.strokeStyle = colors?.[datasetIndex] || "#7fc0ff";
                ctx.globalAlpha = bright
                    ? (datasetIndex === 0 ? 0.95 : 0.82)
                    : (datasetIndex === 0 ? 0.32 : 0.24);
                const peaks = dataset.peaks;
                const datasetDuration = assetDurationSeconds(assetList[datasetIndex])
                    || mediaDurationSeconds(mediaList[datasetIndex], total)
                    || total;
                const visibleStartTime = viewStart * total;
                const visibleEndTime = viewEnd * total;
                const peakStart = Math.max(0, Math.floor((visibleStartTime / datasetDuration) * peaks.length));
                const peakEnd = Math.min(peaks.length, Math.ceil((visibleEndTime / datasetDuration) * peaks.length));
                const midY = row.y + row.height / 2;
                for (let i = peakStart; i < peakEnd; i++) {
                    const [minVal, maxVal] = peaks[i];
                    const time = (i / Math.max(1, peaks.length - 1)) * datasetDuration;
                    const x = ((time / total - viewStart) / visibleRange) * width;
                    if (x < -2 || x > width + 2) continue;
                    const y1 = clamp(row.y + (1 - ((maxVal + 1) / 2)) * row.height, row.y, row.y + row.height);
                    const y2 = clamp(row.y + (1 - ((minVal + 1) / 2)) * row.height, row.y, row.y + row.height);
                    ctx.beginPath();
                    ctx.moveTo(x, y1);
                    ctx.lineTo(x, y2);
                    ctx.stroke();
                }
                if (stackedLayout) {
                    ctx.globalAlpha = 0.8;
                    ctx.fillStyle = colors?.[datasetIndex] || "#7fc0ff";
                    ctx.font = "10px sans-serif";
                    ctx.fillText(row.label, 8, row.y + 14);
                    ctx.globalAlpha = 0.22;
                    ctx.strokeStyle = "#ffffff";
                    ctx.beginPath();
                    ctx.moveTo(0, midY);
                    ctx.lineTo(width, midY);
                    ctx.stroke();
                    const endRatio = datasetDuration / total;
                    const endX = ((endRatio - viewStart) / visibleRange) * width;
                    if (endX > 0 && endX < width) {
                        ctx.fillStyle = "rgba(255,255,255,0.06)";
                        ctx.fillRect(endX, row.y, width - endX, row.height);
                        ctx.globalAlpha = 0.55;
                        ctx.strokeStyle = colors?.[datasetIndex] || "#7fc0ff";
                        ctx.beginPath();
                        ctx.moveTo(endX, row.y);
                        ctx.lineTo(endX, row.y + row.height);
                        ctx.stroke();
                    }
                }
                ctx.globalAlpha = 1;
                ctx.restore();
            };
            const drawPass = (clipStartX, clipEndX, bright) => {
                datasets.forEach((dataset, datasetIndex) => {
                    drawDataset(dataset, datasetIndex, rowForDataset(datasetIndex), bright, clipStartX, clipEndX);
                });
            };

            drawPass(0, width, false);
            if (progressCanvasX > 0) {
                drawPass(0, clamp(progressCanvasX, 0, width), true);
            }

            if (progressCanvasX >= 0 && progressCanvasX <= width) {
                ctx.strokeStyle = "#ffffff";
                ctx.lineWidth = 1.5;
                ctx.globalAlpha = 0.9;
                ctx.beginPath();
                ctx.moveTo(progressCanvasX, 0);
                ctx.lineTo(progressCanvasX, height);
                ctx.stroke();
                ctx.lineWidth = 1;
                ctx.globalAlpha = 1;
            }

            updateTimeLabel();
        };

        const seekFromClientX = (clientX) => {
            const total = duration();
            if (!total) return;
            const rect = canvas.getBoundingClientRect();
            const canvasRatio = clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1);
            const visibleRange = 1 / zoomLevel;
            const timeRatio = clamp(viewOffset + canvasRatio * visibleRange, 0, 1);
            const nextTime = timeRatio * total;
            if (transport) {
                transport.seek(nextTime);
            } else {
                for (const media of mediaList) {
                    media.currentTime = nextTime;
                }
            }
            draw();
        };

        const handlePointerMove = (event) => { if (dragging) seekFromClientX(event.clientX); };
        const handlePointerUp = (event) => {
            if (!dragging) return;
            dragging = false;
            seekFromClientX(event.clientX);
            window.removeEventListener("mousemove", handlePointerMove);
            window.removeEventListener("mouseup", handlePointerUp);
        };
        const handlePointerDown = (event) => {
            event.preventDefault();
            dragging = true;
            seekFromClientX(event.clientX);
            window.addEventListener("mousemove", handlePointerMove);
            window.addEventListener("mouseup", handlePointerUp);
        };
        const handleContextMenu = (event) => {
            event.preventDefault();
        };
        canvas.addEventListener("mousedown", handlePointerDown);
        wrap.addEventListener("contextmenu", handleContextMenu);

        const handleWheel = enableZoom ? (event) => {
            event.preventDefault();
            const rect = canvas.getBoundingClientRect();
            const cursorRatio = (event.clientX - rect.left) / Math.max(1, rect.width);
            const visibleRange = 1 / zoomLevel;
            const cursorTime = viewOffset + cursorRatio * visibleRange;
            const factor = event.deltaY < 0 ? 1.2 : (1 / 1.2);
            zoomLevel = clamp(zoomLevel * factor, 1, 32);
            const newVisibleRange = 1 / zoomLevel;
            viewOffset = clamp(cursorTime - cursorRatio * newVisibleRange, 0, Math.max(0, 1 - newVisibleRange));
            draw();
        } : null;
        if (handleWheel) canvas.addEventListener("wheel", handleWheel, { passive: false });

        const ensurePlayheadVisible = () => {
            if (zoomLevel <= 1) return;
            const total = duration();
            if (!total) return;
            const ratio = currentTime() / total;
            const visibleRange = 1 / zoomLevel;
            const viewEnd = viewOffset + visibleRange;
            if (ratio < viewOffset) viewOffset = ratio;
            else if (ratio > viewEnd) viewOffset = clamp(ratio - visibleRange * 0.9, 0, Math.max(0, 1 - visibleRange));
        };

        const handleTimeUpdate = () => {
            if (!dragging) {
                ensurePlayheadVisible();
                draw();
            }
        };

        for (const media of mediaList) {
            media.addEventListener?.("timeupdate", handleTimeUpdate);
            media.addEventListener?.("loadedmetadata", draw);
            media.addEventListener?.("durationchange", draw);
            media.addEventListener?.("seeked", draw);
            media.addEventListener?.("ended", draw);
        }
        const unsubscribeTransport = transport?.subscribe?.(() => {
            if (!dragging) {
                ensurePlayheadVisible();
                draw();
            }
        }) || null;

        Promise.all(assetList.map(async (asset) => {
            try {
                const resp = await fetch(buildWaveformUrl(currentProjectDir(), asset.asset_id));
                return resp.ok ? await resp.json() : null;
            } catch {
                return null;
            }
        })).then((results) => {
            if (!active) return;
            datasets = results;
            if (!results.some((d) => d?.peaks?.length)) {
                status.textContent = "Waveform unavailable.";
                return;
            }
            status.textContent = assetList.length > 1
                ? assetList.map((a, i) => `${assetDisplayName(a)}: ${colors?.[i] || "#7fc0ff"}`).join(" | ")
                : assetDisplayName(assetList[0]);
            requestAnimationFrame(() => draw());
        });

        updateTimeLabel();

        return {
            el: wrap,
            cleanup() {
                active = false;
                dragging = false;
                window.removeEventListener("mousemove", handlePointerMove);
                window.removeEventListener("mouseup", handlePointerUp);
                canvas.removeEventListener("mousedown", handlePointerDown);
                wrap.removeEventListener("contextmenu", handleContextMenu);
                if (handleWheel) canvas.removeEventListener("wheel", handleWheel);
                unsubscribeTransport?.();
                for (const media of mediaList) {
                    media.removeEventListener?.("timeupdate", handleTimeUpdate);
                    media.removeEventListener?.("loadedmetadata", draw);
                    media.removeEventListener?.("durationchange", draw);
                    media.removeEventListener?.("seeked", draw);
                    media.removeEventListener?.("ended", draw);
                }
            },
        };
    }

    function cycleOverlayAsset(direction) {
        const assets = overlayAssets();
        if (!assets.length) return;
        const currentIndex = assets.findIndex((asset) => asset.asset_id === state.overlayState.assetId);
        const safeIndex = currentIndex >= 0 ? currentIndex : 0;
        const nextIndex = (safeIndex + direction + assets.length) % assets.length;
        state.overlayState.assetId = assets[nextIndex].asset_id;
        state.overlayState.compareMode = false;
        state.overlayState.showWaveform = false;
        resetOverlayTransform();
        renderInspectOverlay();
    }

    function ensureCompareDefaults(asset) {
        const candidates = sameTypeOverlayAssets(asset);
        if (!candidates.length) return;
        state.overlayState.compareLeftAssetId = state.overlayState.compareLeftAssetId || asset.asset_id;
        if (!state.overlayState.compareRightAssetId || state.overlayState.compareRightAssetId === state.overlayState.compareLeftAssetId) {
            state.overlayState.compareRightAssetId = candidates.find((entry) => entry.asset_id !== state.overlayState.compareLeftAssetId)?.asset_id || state.overlayState.compareLeftAssetId;
        }
    }

    function openInspectOverlay(asset) {
        if (!asset || isTrashed(asset)) return;
        state.overlayState.open = true;
        state.overlayState.assetId = asset.asset_id;
        state.overlayState.compareMode = false;
        state.overlayState.showMetadata = false;
        state.overlayState.compareLeftAssetId = asset.asset_id;
        state.overlayState.compareRightAssetId = "";
        state.overlayState.comparePickerQuery = "";
        state.overlayState.comparePickerSortMode = DEFAULT_SORT_MODE;
        state.overlayState.dividerRatio = 0.5;
        state.overlayState.audioFocus = "none";
        state.overlayState.audioTempFlip = false;
        state.overlayState.showWaveform = false;
        resetOverlayTransform();
        renderInspectOverlay();
    }

    function renderSingleOverlay(asset, host) {
        const projectDir = currentProjectDir();
        const content = style(document.createElement("div"), `display:flex;flex-direction:column;gap:12px;flex:1 1 auto;min-height:0;`);
        host.appendChild(content);

        if (assetIsMissing(asset)) {
            const missing = style(document.createElement("div"), `margin:auto;color:#f0b39f;font-size:13px;`);
            missing.textContent = "Missing asset preview unavailable.";
            content.appendChild(missing);
            return;
        }

        if (asset.asset_type === "image") {
            const stage = style(document.createElement("div"), `position:relative;flex:1 1 auto;min-height:0;border-radius:12px;background:#020507;border:1px solid #24323e;display:flex;align-items:center;justify-content:center;overflow:hidden;`);
            const img = style(document.createElement("img"), `max-width:100%;max-height:100%;display:block;user-select:none;pointer-events:none;`);
            img.draggable = false;
            img.src = buildAssetViewUrl(projectDir, asset.path);
            img.alt = assetDisplayName(asset);
            stage.appendChild(img);
            state.overlayState.cleanupFns.push(attachZoomPan(stage, img));
            content.appendChild(stage);
            return;
        }

        if (asset.asset_type === "video") {
            const stage = style(document.createElement("div"), `position:relative;flex:1 1 auto;min-height:0;border-radius:12px;background:#020507;border:1px solid #24323e;display:flex;align-items:center;justify-content:center;overflow:hidden;`);
            const video = style(document.createElement("video"), `width:100%;height:100%;object-fit:contain;display:block;background:#000;user-select:none;`);
            video.draggable = false;
            video.preload = "auto";
            video.playsInline = true;
            if (asset.has_thumbnail) video.poster = buildThumbnailUrl(projectDir, asset.asset_id);
            const blobHandle = loadMediaAsBlob(buildAssetViewUrl(projectDir, asset.path), video);
            const togglePlayback = () => {
                if (video.paused) {
                    void video.play();
                } else {
                    video.pause();
                }
            };
            state.overlayState.togglePlayback = togglePlayback;
            const playbackRow = style(document.createElement("div"), `display:flex;align-items:center;gap:8px;flex-wrap:wrap;`);
            const playPauseBtn = makeActionButton("subtle");
            const playbackHint = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;`);
            playbackHint.textContent = "Space = Play / Pause";
            const syncPlayPauseLabel = () => {
                playPauseBtn.textContent = video.paused ? "Play" : "Pause";
            };
            syncPlayPauseLabel();
            playPauseBtn.addEventListener("click", togglePlayback);
            video.addEventListener("play", syncPlayPauseLabel);
            video.addEventListener("pause", syncPlayPauseLabel);
            video.addEventListener("ended", syncPlayPauseLabel);
            playbackRow.append(playPauseBtn, playbackHint);
            const scrub = renderMediaScrubBar(video);
            stage.appendChild(video);
            content.append(stage, playbackRow, scrub.el);
            state.overlayState.cleanupFns.push(
                attachZoomPan(stage, video),
                scrub.cleanup,
                blobHandle.cleanup,
                () => {
                    playPauseBtn.removeEventListener("click", togglePlayback);
                    video.removeEventListener("play", syncPlayPauseLabel);
                    video.removeEventListener("pause", syncPlayPauseLabel);
                    video.removeEventListener("ended", syncPlayPauseLabel);
                },
            );
            if (asset.has_audio) {
                const toggleBtn = makeActionButton();
                toggleBtn.textContent = state.overlayState.showWaveform ? "Hide Waveform" : "Show Waveform";
                toggleBtn.style.background = "#1d2630";
                toggleBtn.style.borderColor = "#364655";
                toggleBtn.addEventListener("click", () => {
                    state.overlayState.showWaveform = !state.overlayState.showWaveform;
                    renderInspectOverlay();
                });
                content.appendChild(toggleBtn);
                if (state.overlayState.showWaveform) {
                    const waveform = renderWaveformPanel(asset, ["#7fc0ff"]);
                    state.overlayState.cleanupFns.push(waveform.cleanup);
                    content.appendChild(waveform.el);
                }
            }
            return;
        }

        if (asset.asset_type === "audio") {
            const card = style(document.createElement("div"), `display:flex;flex-direction:column;gap:12px;max-width:900px;width:100%;margin:auto;padding:18px;border-radius:12px;background:#04090d;border:1px solid #24323e;`);
            const audio = document.createElement("audio");
            audio.preload = "auto";
            const blobHandle = loadMediaAsBlob(buildAssetViewUrl(projectDir, asset.path), audio);
            const toggleAudio = () => {
                if (audio.paused) void audio.play();
                else audio.pause();
            };
            state.overlayState.togglePlayback = toggleAudio;
            const playBtn = makeActionButton();
            playBtn.textContent = "Play / Pause";
            playBtn.addEventListener("click", toggleAudio);
            const waveform = renderInteractiveWaveform(asset, audio, ["#7fc0ff"], { compact: true });
            card.append(playBtn, waveform.el);
            state.overlayState.cleanupFns.push(
                waveform.cleanup,
                blobHandle.cleanup,
                () => {
                    playBtn.removeEventListener("click", toggleAudio);
                    audio.pause();
                },
            );
            content.appendChild(card);
        }
    }

    function renderCompareChooser(assets, slotLabel, selectedId, onAssign, requestRefresh = () => {}) {
        const wrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:8px;min-width:0;height:100%;padding:10px;border-radius:12px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.08);overflow:hidden;`);
        const title = style(document.createElement("div"), `color:#d7e5f1;font-size:11px;font-weight:700;letter-spacing:0.04em;text-transform:uppercase;`);
        title.textContent = slotLabel;
        const hint = style(document.createElement("div"), `color:#8ea0af;font-size:10px;line-height:1.45;`);
        hint.textContent = "Left click assigns A. Right click assigns B.";
        const controls = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;`);
        const search = style(document.createElement("input"), `width:100%;box-sizing:border-box;${inputChromeCss()}`);
        search.type = "search";
        search.placeholder = "Search assets (kind:/ext:/tracked:/field:)";
        search.value = state.overlayState.comparePickerQuery || "";
        const sort = style(document.createElement("select"), `width:100%;box-sizing:border-box;${inputChromeCss()}`);
        for (const option of COMPARE_SORT_OPTIONS) {
            const optionEl = document.createElement("option");
            optionEl.value = option.value;
            optionEl.textContent = option.label;
            sort.appendChild(optionEl);
        }
        sort.value = COMPARE_SORT_OPTIONS.some((entry) => entry.value === state.overlayState.comparePickerSortMode)
            ? state.overlayState.comparePickerSortMode
            : DEFAULT_SORT_MODE;
        controls.append(search, sort);
        const list = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;overflow:auto;min-height:0;padding-right:2px;`);

        const chooserAssets = () => {
            const query = parseAssetSearchQuery(state.overlayState.comparePickerQuery || "");
            const filtered = sortAssetsByMode(
                assets.filter((entry) => assetMatchesParsedQuery(entry, query)),
                state.overlayState.comparePickerSortMode || DEFAULT_SORT_MODE,
            );
            const pinnedIds = [
                state.overlayState.compareLeftAssetId,
                state.overlayState.compareRightAssetId,
            ].filter(Boolean);
            const result = [];
            const seen = new Set();
            for (const assetId of pinnedIds) {
                const pinned = assets.find((entry) => entry.asset_id === assetId);
                if (pinned && !seen.has(pinned.asset_id)) {
                    result.push(pinned);
                    seen.add(pinned.asset_id);
                }
            }
            for (const entry of filtered) {
                if (!seen.has(entry.asset_id)) {
                    result.push(entry);
                    seen.add(entry.asset_id);
                }
            }
            return result;
        };

        const renderRows = () => {
            list.innerHTML = "";
            search.value = state.overlayState.comparePickerQuery || "";
            sort.value = COMPARE_SORT_OPTIONS.some((entry) => entry.value === state.overlayState.comparePickerSortMode)
                ? state.overlayState.comparePickerSortMode
                : DEFAULT_SORT_MODE;
            for (const asset of chooserAssets()) {
                const pinned = asset.asset_id === state.overlayState.compareLeftAssetId || asset.asset_id === state.overlayState.compareRightAssetId;
                const row = style(document.createElement("div"), `display:grid;grid-template-columns:52px minmax(0,1fr);gap:8px;align-items:center;padding:6px;border-radius:8px;border:1px solid ${selectedId === asset.asset_id ? "#7fc0ff" : "#35414c"};background:${selectedId === asset.asset_id ? "rgba(78,121,160,0.18)" : "rgba(255,255,255,0.02)"};cursor:pointer;`);
                const thumb = style(document.createElement("div"), `height:40px;border-radius:6px;background:#111;border:1px solid #293542;display:flex;align-items:center;justify-content:center;overflow:hidden;color:#7f93a5;font-size:10px;`);
                if (asset.has_thumbnail) {
                    const img = style(document.createElement("img"), `width:100%;height:100%;object-fit:cover;display:block;`);
                    img.src = buildThumbnailUrl(currentProjectDir(), asset.asset_id);
                    thumb.appendChild(img);
                } else {
                    thumb.textContent = assetFallbackGlyph(asset.asset_type);
                }
                const text = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:2px;`);
                const name = style(document.createElement("div"), `color:#edf3f8;font-size:11px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                name.textContent = assetDisplayName(asset);
                const meta = style(document.createElement("div"), `color:#8ea0af;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                const pinLabel = pinned ? "Pinned | " : "";
                meta.textContent = `${pinLabel}${assetKindLabel(asset.asset_type)} | ${formatDuration(asset)}`;
                text.append(name, meta);
                row.append(thumb, text);
                row.addEventListener("click", () => onAssign("left", asset.asset_id));
                row.addEventListener("contextmenu", (event) => {
                    event.preventDefault();
                    onAssign("right", asset.asset_id);
                });
                list.appendChild(row);
            }
            if (!list.children.length) {
                const empty = style(document.createElement("div"), `color:${CHROME.textDim};font-size:11px;padding:8px;`);
                empty.textContent = "No matching assets.";
                list.appendChild(empty);
            }
        };

        search.addEventListener("input", () => {
            state.overlayState.comparePickerQuery = search.value || "";
            requestRefresh();
        });
        sort.addEventListener("change", () => {
            state.overlayState.comparePickerSortMode = sort.value || DEFAULT_SORT_MODE;
            requestRefresh();
        });
        renderRows();
        wrap.append(title, hint, controls, list);
        return { el: wrap, refresh: renderRows };
    }

    function renderCompareOverlay(asset, host) {
        const candidates = sameTypeOverlayAssets(asset);
        const compareA = candidates.find((entry) => entry.asset_id === state.overlayState.compareLeftAssetId) || asset;
        const compareB = candidates.find((entry) => entry.asset_id === state.overlayState.compareRightAssetId) || candidates.find((entry) => entry.asset_id !== compareA.asset_id) || compareA;

        const layout = style(document.createElement("div"), `display:grid;grid-template-columns:minmax(220px,260px) minmax(0,1fr) minmax(220px,260px);gap:12px;flex:1 1 auto;min-height:0;width:100%;`);
        const assignSlot = (slot, assetId) => {
            if (slot === "left") {
                state.overlayState.compareLeftAssetId = assetId;
            } else {
                state.overlayState.compareRightAssetId = assetId;
            }
            renderInspectOverlay();
        };
        let refreshCompareChoosers = () => {};
        const chooserA = renderCompareChooser(candidates, "Gallery A", compareA.asset_id, assignSlot, () => refreshCompareChoosers());
        const chooserB = renderCompareChooser(candidates, "Gallery B", compareB.asset_id, assignSlot, () => refreshCompareChoosers());
        refreshCompareChoosers = () => {
            chooserA.refresh();
            chooserB.refresh();
        };

        layout.appendChild(chooserA.el);

        const center = style(document.createElement("div"), `display:flex;flex-direction:column;gap:12px;min-width:0;min-height:0;`);
        const hint = style(document.createElement("div"), `color:#8ea0af;font-size:11px;flex:0 0 auto;`);
        hint.textContent = "Left click = A | Right click = B";
        center.appendChild(hint);

        const makeSegmentButton = (label, active, onClick, title = "") => {
            const btn = makeActionButton(active ? "active" : "subtle");
            btn.textContent = label;
            if (title) btn.title = title;
            btn.addEventListener("click", onClick);
            return btn;
        };
        const setInspectorSettingAndRender = (key, value) => {
            state.overlayState[key] = value;
            persistInspectorSetting(key, value);
            renderInspectOverlay();
        };

        if (asset.asset_type === "audio") {
            const controls = style(document.createElement("div"), `display:flex;align-items:center;gap:8px;flex-wrap:wrap;`);
            const audioA = new Audio();
            const audioB = new Audio();
            const blobHandleA = loadMediaAsBlob(buildAssetViewUrl(currentProjectDir(), compareA.path), audioA);
            const blobHandleB = loadMediaAsBlob(buildAssetViewUrl(currentProjectDir(), compareB.path), audioB);
            audioA.preload = "auto";
            audioB.preload = "auto";
            const transport = createLinkedMediaTransport([audioA, audioB], {
                fallbackDurations: [assetDurationSeconds(compareA), assetDurationSeconds(compareB)],
            });
            state.overlayState.togglePlayback = () => transport.toggle();
            const playBtn = makeActionButton("subtle");
            playBtn.textContent = "Play";
            playBtn.addEventListener("click", () => transport.toggle());
            controls.appendChild(playBtn);
            const monitorButtons = [];
            const effectiveMonitor = () => {
                const monitor = state.overlayState.audioCompareMonitor || DEFAULT_INSPECTOR_SETTINGS.audioCompareMonitor;
                if (state.overlayState.audioTempFlip && monitor === "a") return "b";
                if (state.overlayState.audioTempFlip && monitor === "b") return "a";
                return monitor;
            };
            const applyAudioMonitor = () => {
                const monitor = effectiveMonitor();
                const both = monitor === "both";
                audioA.muted = !(monitor === "a" || both);
                audioB.muted = !(monitor === "b" || both);
                audioA.volume = both ? AUDIO_DUCK_VOLUME : 1;
                audioB.volume = both ? AUDIO_DUCK_VOLUME : 1;
                for (const [btn, value] of monitorButtons) {
                    btn.style.cssText = actionButtonCss(state.overlayState.audioCompareMonitor === value ? "active" : "subtle");
                }
            };
            state.overlayState.applyAudioMonitor = applyAudioMonitor;
            for (const [labelText, value] of [["A", "a"], ["B", "b"], ["Both", "both"], ["Mute", "mute"]]) {
                const btn = makeActionButton(state.overlayState.audioCompareMonitor === value ? "active" : "subtle");
                btn.textContent = labelText;
                btn.title = `Monitor ${labelText}`;
                btn.addEventListener("click", () => {
                    state.overlayState.audioCompareMonitor = value;
                    persistInspectorSetting("audioCompareMonitor", value);
                    applyAudioMonitor();
                });
                monitorButtons.push([btn, value]);
                controls.appendChild(btn);
            }
            controls.append(
                makeSegmentButton("Stacked", state.overlayState.audioCompareWaveformLayout === "stacked", () => setInspectorSettingAndRender("audioCompareWaveformLayout", "stacked")),
                makeSegmentButton("Overlay", state.overlayState.audioCompareWaveformLayout === "overlay", () => setInspectorSettingAndRender("audioCompareWaveformLayout", "overlay")),
            );
            const syncPlayPauseLabel = ({ playing }) => {
                playBtn.textContent = playing ? "Pause" : "Play";
            };
            const transportOff = transport.subscribe(syncPlayPauseLabel);
            applyAudioMonitor();
            const waveform = renderInteractiveWaveform([compareA, compareB], [audioA, audioB], ["#7fc0ff", "#f5a97a"], {
                enableZoom: true,
                layout: state.overlayState.audioCompareWaveformLayout,
                transport,
            });
            state.overlayState.cleanupFns.push(
                waveform.cleanup,
                transportOff,
                transport.cleanup,
                blobHandleA.cleanup,
                blobHandleB.cleanup,
                () => { audioA.pause(); audioB.pause(); },
            );
            center.append(controls, waveform.el);
        } else {
            const controls = style(document.createElement("div"), `display:flex;align-items:center;gap:8px;flex-wrap:wrap;`);
            controls.append(
                makeSegmentButton("Divider", state.overlayState.compareLayout === "divider", () => setInspectorSettingAndRender("compareLayout", "divider")),
                makeSegmentButton("Side by Side", state.overlayState.compareLayout === "sideBySide", () => setInspectorSettingAndRender("compareLayout", "sideBySide")),
            );
            if (state.overlayState.compareLayout === "sideBySide") {
                controls.appendChild(makeSegmentButton(
                    state.overlayState.sideBySideLinkZoom ? "Linked Zoom" : "Independent Zoom",
                    state.overlayState.sideBySideLinkZoom,
                    () => setInspectorSettingAndRender("sideBySideLinkZoom", !state.overlayState.sideBySideLinkZoom),
                    "Toggle side-by-side zoom/pan linking",
                ));
            }
            center.appendChild(controls);

            const layerA = asset.asset_type === "image" ? document.createElement("img") : document.createElement("video");
            const layerB = asset.asset_type === "image" ? document.createElement("img") : document.createElement("video");
            const mediaStyle = `position:absolute;inset:0;width:100%;height:100%;object-fit:contain;display:block;background:#000;pointer-events:none;user-select:none;`;
            layerA.style.cssText = mediaStyle;
            layerB.style.cssText = mediaStyle;
            layerA.draggable = false;
            layerB.draggable = false;
            if (asset.asset_type === "image") {
                layerA.src = buildAssetViewUrl(currentProjectDir(), compareA.path);
                layerB.src = buildAssetViewUrl(currentProjectDir(), compareB.path);
                layerA.alt = assetDisplayName(compareA);
                layerB.alt = assetDisplayName(compareB);
            } else {
                layerA.preload = "auto";
                layerB.preload = "auto";
                layerA.playsInline = true;
                layerB.playsInline = true;
                const blobHandleA = loadMediaAsBlob(buildAssetViewUrl(currentProjectDir(), compareA.path), layerA);
                const blobHandleB = loadMediaAsBlob(buildAssetViewUrl(currentProjectDir(), compareB.path), layerB);
                state.overlayState.cleanupFns.push(blobHandleA.cleanup, blobHandleB.cleanup);
                layerA.muted = state.overlayState.audioFocus !== "a";
                layerB.muted = state.overlayState.audioFocus !== "b";
            }
            const groupStyle = `position:absolute;inset:0;width:100%;height:100%;pointer-events:none;`;
            const contentGroupA = style(document.createElement("div"), groupStyle);
            const contentGroupB = style(document.createElement("div"), groupStyle);
            contentGroupA.appendChild(layerA);
            contentGroupB.appendChild(layerB);
            let transport = null;
            if (asset.asset_type === "video") {
                state.overlayState.videoCompareFps = assetFps(compareA);
                transport = createLinkedMediaTransport([layerA, layerB], {
                    fallbackDurations: [assetDurationSeconds(compareA), assetDurationSeconds(compareB)],
                });
                state.overlayState.togglePlayback = () => transport.toggle();
                state.overlayState.stepVideoCompare = (deltaSeconds) => transport.step(deltaSeconds);
                state.overlayState.cleanupFns.push(transport.cleanup);
            }

            if (state.overlayState.compareLayout === "sideBySide") {
                const stage = style(document.createElement("div"), `display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px;flex:1 1 auto;min-height:0;`);
                const makePane = (label, group, slot) => {
                    const pane = style(document.createElement("div"), `position:relative;min-width:0;min-height:0;border-radius:12px;background:#020507;border:1px solid #24323e;overflow:hidden;display:flex;align-items:center;justify-content:center;`);
                    const badge = style(document.createElement("div"), `position:absolute;left:10px;top:10px;z-index:2;padding:3px 6px;border-radius:5px;background:rgba(0,0,0,0.58);color:${CHROME.text};font-size:10px;pointer-events:none;`);
                    badge.textContent = label;
                    pane.append(group, badge);
                    if (asset.asset_type === "video") {
                        state.overlayState.cleanupFns.push(attachRightClickVideoScrub(pane, transport, [layerA, layerB], { fps: assetFps(compareA) }));
                    }
                    return pane;
                };
                const paneA = makePane("A", contentGroupA, "a");
                const paneB = makePane("B", contentGroupB, "b");
                stage.append(paneA, paneB);
                center.appendChild(stage);
                if (state.overlayState.sideBySideLinkZoom) {
                    state.overlayState.cleanupFns.push(
                        attachZoomPan(paneA, [contentGroupA, contentGroupB], { transformState: state.overlayState }),
                        attachZoomPan(paneB, [contentGroupA, contentGroupB], { transformState: state.overlayState }),
                    );
                } else {
                    state.overlayState.cleanupFns.push(
                        attachZoomPan(paneA, contentGroupA, { transformState: state.overlayState.sideBySideTransforms.a }),
                        attachZoomPan(paneB, contentGroupB, { transformState: state.overlayState.sideBySideTransforms.b }),
                    );
                }
            } else {
                const stage = style(document.createElement("div"), `position:relative;flex:1 1 auto;min-height:0;border-radius:12px;background:#020507;border:1px solid #24323e;overflow:hidden;display:flex;align-items:center;justify-content:center;`);
                const clipWrapperB = style(document.createElement("div"), `position:absolute;inset:0;pointer-events:none;`);
                clipWrapperB.appendChild(contentGroupB);
                const divider = style(document.createElement("div"), `position:absolute;top:0;bottom:0;left:50%;width:14px;transform:translateX(-50%);cursor:col-resize;pointer-events:auto;z-index:3;`);
                const dividerLine = style(document.createElement("div"), `position:absolute;top:0;bottom:0;left:50%;width:2px;transform:translateX(-50%);background:#f1f5f8;box-shadow:0 0 0 1px rgba(0,0,0,0.35);`);
                divider.appendChild(dividerLine);
                stage.appendChild(contentGroupA);
                stage.appendChild(clipWrapperB);
                stage.appendChild(divider);
                const applyDivider = () => {
                    const ratio = clamp(state.overlayState.dividerRatio, 0, 1);
                    const leftPct = `${ratio * 100}%`;
                    clipWrapperB.style.clipPath = `inset(0 0 0 ${leftPct})`;
                    divider.style.left = leftPct;
                    divider.style.transform = "translateX(-50%)";
                };
                applyDivider();
                const handleDividerMove = (event) => {
                    const rect = stage.getBoundingClientRect();
                    const ratio = (event.clientX - rect.left) / Math.max(1, rect.width);
                    state.overlayState.dividerRatio = clamp(ratio, 0, 1);
                    applyDivider();
                };
                divider.addEventListener("mousedown", (event) => {
                    event.stopPropagation();
                }, true);
                const handleDividerPointerDown = (event) => {
                    if (event.button !== 0) return;
                    event.preventDefault();
                    event.stopPropagation();
                    divider.setPointerCapture?.(event.pointerId);
                    const onMove = (e) => handleDividerMove(e);
                    const onUp = (e) => {
                        divider.releasePointerCapture?.(e.pointerId);
                        divider.removeEventListener("pointermove", onMove);
                        divider.removeEventListener("pointerup", onUp);
                        divider.removeEventListener("pointercancel", onUp);
                    };
                    divider.addEventListener("pointermove", onMove);
                    divider.addEventListener("pointerup", onUp);
                    divider.addEventListener("pointercancel", onUp);
                };
                divider.addEventListener("pointerdown", handleDividerPointerDown);
                const handleResize = () => applyDivider();
                window.addEventListener("resize", handleResize);
                state.overlayState.cleanupFns.push(
                    () => window.removeEventListener("resize", handleResize),
                    attachZoomPan(stage, [contentGroupA, contentGroupB]),
                );
                if (asset.asset_type === "video") {
                    state.overlayState.cleanupFns.push(attachRightClickVideoScrub(stage, transport, [layerA, layerB], { fps: assetFps(compareA) }));
                }
                center.appendChild(stage);
            }

            if (asset.asset_type === "video") {
                const videoControls = style(document.createElement("div"), `display:flex;align-items:center;gap:8px;flex-wrap:wrap;`);
                const playPauseBtn = makeActionButton("subtle");
                const playbackHint = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;`);
                playbackHint.textContent = "Space = Play / Pause | Right-drag = scrub";
                const handlePlayPauseClick = () => transport.toggle();
                const syncPlayPauseLabel = ({ playing }) => {
                    playPauseBtn.textContent = playing ? "Pause" : "Play";
                };
                const transportOff = transport.subscribe(syncPlayPauseLabel);
                playPauseBtn.addEventListener("click", handlePlayPauseClick);
                videoControls.append(playPauseBtn, playbackHint);
                const applyVideoAudioFocus = () => {
                    layerA.muted = state.overlayState.audioFocus !== "a";
                    layerB.muted = state.overlayState.audioFocus !== "b";
                };
                for (const [labelText, value] of [["A", "a"], ["B", "b"], ["None", "none"]]) {
                    const btn = makeActionButton(state.overlayState.audioFocus === value ? "active" : "subtle");
                    btn.textContent = labelText;
                    btn.addEventListener("click", () => {
                        state.overlayState.audioFocus = value;
                        applyVideoAudioFocus();
                        renderInspectOverlay();
                    });
                    videoControls.appendChild(btn);
                }
                applyVideoAudioFocus();
                const scrub = renderSynchronizedScrubBar([layerA, layerB], { transport });
                state.overlayState.cleanupFns.push(
                    transportOff,
                    scrub.cleanup,
                    () => {
                        playPauseBtn.removeEventListener("click", handlePlayPauseClick);
                        layerA.pause();
                        layerB.pause();
                    },
                );
                center.append(videoControls, scrub.el);
            }
        }

        layout.appendChild(center);
        layout.appendChild(chooserB.el);
        host.appendChild(layout);
    }

    function renderInspectOverlay() {
        const overlay = state.overlayState;
        const asset = currentOverlayAsset();
        if (!overlay.open || !asset) {
            closeInspectOverlay();
            return;
        }

        clearOverlayRuntime();
        let overlayEl = overlay.overlayEl;
        if (!overlayEl) {
            overlayEl = style(document.createElement("div"), `position:fixed;inset:0;z-index:99999;background:rgba(7,10,14,0.86);display:flex;align-items:stretch;justify-content:center;padding:20px;box-sizing:border-box;`);
            overlayEl.addEventListener("mousedown", (event) => {
                if (event.target === overlayEl) {
                    closeInspectOverlay();
                }
            });
            document.body.appendChild(overlayEl);
            overlay.overlayEl = overlayEl;
        }
        overlayEl.dataset.sonderInspectOverlay = "1";

        const applyCenterZoom = (factor) => {
            if (state.overlayState.compareMode && state.overlayState.compareLayout === "sideBySide" && !state.overlayState.sideBySideLinkZoom) {
                let changed = false;
                for (const transformState of [state.overlayState.sideBySideTransforms.a, state.overlayState.sideBySideTransforms.b]) {
                    const previousZoom = transformState.zoomLevel;
                    const nextZoom = clamp(previousZoom * factor, 1, 16);
                    if (nextZoom === previousZoom) continue;
                    if (nextZoom < previousZoom) {
                        const denom = Math.max(0.0001, previousZoom - 1);
                        const ratio = Math.max(0, (nextZoom - 1) / denom);
                        transformState.panX *= ratio;
                        transformState.panY *= ratio;
                    }
                    transformState.zoomLevel = nextZoom;
                    changed = true;
                }
                if (changed) renderInspectOverlay();
                return;
            }
            const previousZoom = state.overlayState.zoomLevel;
            const nextZoom = clamp(previousZoom * factor, 1, 16);
            if (nextZoom === previousZoom) return;
            if (nextZoom > previousZoom) {
                // center pivot — no pan adjustment needed since transform-origin is center
            } else {
                const denom = Math.max(0.0001, previousZoom - 1);
                const ratio = Math.max(0, (nextZoom - 1) / denom);
                state.overlayState.panX *= ratio;
                state.overlayState.panY *= ratio;
            }
            state.overlayState.zoomLevel = nextZoom;
            renderInspectOverlay();
        };
        const keyDownHandler = (event) => {
            if (!overlay.open) return false;
            if (event.target?.closest?.("input, textarea, select")) return false;
            const activeAsset = currentOverlayAsset();
            const compareVideo = overlay.compareMode && activeAsset?.asset_type === "video";
            const compareAudio = overlay.compareMode && activeAsset?.asset_type === "audio";
            if (compareVideo && (event.key === "ArrowLeft" || event.key === "ArrowRight") && typeof overlay.stepVideoCompare === "function") {
                const direction = event.key === "ArrowLeft" ? -1 : 1;
                const fps = Number(overlay.videoCompareFps) || assetFps(activeAsset);
                const seconds = event.ctrlKey || event.metaKey
                    ? 1
                    : (event.shiftKey ? 10 / fps : 1 / fps);
                overlay.stepVideoCompare(direction * seconds);
                return true;
            }
            if (compareAudio && event.key === "Shift") {
                const monitor = overlay.audioCompareMonitor;
                if (monitor === "a" || monitor === "b") {
                    overlay.audioTempFlip = true;
                    overlay.applyAudioMonitor?.();
                    return true;
                }
            }
            if (compareAudio) {
                const monitorKeyMap = { "1": "a", "2": "b", "3": "both", "0": "mute" };
                const nextMonitor = monitorKeyMap[event.key];
                if (nextMonitor) {
                    overlay.audioCompareMonitor = nextMonitor;
                    persistInspectorSetting("audioCompareMonitor", nextMonitor);
                    overlay.applyAudioMonitor?.();
                    return true;
                }
            }
            if (event.ctrlKey || event.metaKey || event.altKey) return shouldCaptureOverlayShortcut(event);
            if (event.key === "Escape") { closeInspectOverlay(); return true; }
            if (event.key === " " || event.key === "Spacebar") {
                if (typeof overlay.togglePlayback === "function") overlay.togglePlayback();
                return true;
            }
            if (event.key === "?") {
                showInspectOverlayShortcutHelp();
                return true;
            }
            if (event.key === "ArrowLeft") { cycleOverlayAsset(-1); return true; }
            if (event.key === "ArrowRight") { cycleOverlayAsset(1); return true; }
            if (event.key === "f" || event.key === "F" || event.key === "0") {
                resetOverlayTransform();
                renderInspectOverlay();
                return true;
            }
            if (event.key === "c" || event.key === "C") {
                const candidates = sameTypeOverlayAssets(currentOverlayAsset());
                if (candidates.length < 2) return true;
                overlay.compareMode = !overlay.compareMode;
                if (overlay.compareMode) ensureCompareDefaults(currentOverlayAsset());
                resetOverlayTransform();
                renderInspectOverlay();
                return true;
            }
            if (event.key === "+" || event.key === "=") {
                applyCenterZoom(1.25);
                return true;
            }
            if (event.key === "-" || event.key === "_") {
                applyCenterZoom(1 / 1.25);
                return true;
            }
            return shouldCaptureOverlayShortcut(event);
        };
        const keyUpHandler = (event) => {
            if (!overlay.open) return false;
            const activeAsset = currentOverlayAsset();
            if (overlay.compareMode && activeAsset?.asset_type === "audio" && event.key === "Shift") {
                if (overlay.audioTempFlip) {
                    overlay.audioTempFlip = false;
                    overlay.applyAudioMonitor?.();
                    return true;
                }
            }
            if (event.target?.closest?.("input, textarea, select")) return false;
            return shouldCaptureOverlayShortcut(event);
        };
        const overlayKeyOff = registerKeyboardConsumer({
            id: consumerId("inspect"),
            priority: KEY_PRIORITY.OVERLAY,
            keydown: keyDownHandler,
            keyup: keyUpHandler,
        });
        overlay.cleanupFns.push(overlayKeyOff);

        overlayEl.innerHTML = "";
        const shell = style(document.createElement("div"), `display:flex;flex-direction:column;gap:12px;max-width:min(1600px,100%);width:100%;height:100%;padding:18px;border-radius:16px;background:${CHROME.panelMuted};border:1px solid ${CHROME.borderStrong};box-shadow:0 20px 80px rgba(0,0,0,0.45);box-sizing:border-box;`);
        const toolbar = style(document.createElement("div"), `display:flex;align-items:center;justify-content:space-between;gap:12px;min-width:0;`);
        const titleWrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:2px;min-width:0;`);
        const title = style(document.createElement("div"), `color:#f1f5f8;font-size:15px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        title.textContent = assetDisplayName(asset);
        const counter = style(document.createElement("div"), `color:${CHROME.textDim};font-size:11px;`);
        const assets = overlayAssets();
        const assetIndex = Math.max(0, assets.findIndex((entry) => entry.asset_id === asset.asset_id));
        counter.textContent = `${assetIndex + 1} / ${Math.max(assets.length, 1)}`;
        titleWrap.append(title, counter);

        const toolbarActions = style(document.createElement("div"), `display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end;`);
        const compareCandidates = sameTypeOverlayAssets(asset);
        const fitBtn = makeActionButton("subtle");
        fitBtn.textContent = "Fit";
        fitBtn.title = "Fit (F / 0)";
        fitBtn.addEventListener("click", () => {
            resetOverlayTransform();
            renderInspectOverlay();
        });
        toolbarActions.appendChild(fitBtn);
        const compareBtn = makeActionButton();
        compareBtn.textContent = overlay.compareMode ? "Compare Off" : "Compare";
        compareBtn.title = compareCandidates.length < 2 ? "Compare needs ≥ 2 same-type visible assets" : "Compare (C)";
        compareBtn.disabled = compareCandidates.length < 2;
        compareBtn.addEventListener("click", () => {
            overlay.compareMode = !overlay.compareMode;
            ensureCompareDefaults(asset);
            resetOverlayTransform();
            renderInspectOverlay();
        });
        toolbarActions.appendChild(compareBtn);

        const metadataBtn = makeActionButton(overlay.showMetadata ? "active" : "subtle");
        metadataBtn.textContent = overlay.showMetadata ? "Metadata On" : "Metadata";
        metadataBtn.title = "Show or hide asset metadata in this inspector";
        metadataBtn.addEventListener("click", () => {
            overlay.showMetadata = !overlay.showMetadata;
            renderInspectOverlay();
        });
        toolbarActions.appendChild(metadataBtn);

        const helpBtn = makeActionButton("subtle");
        helpBtn.textContent = "?";
        helpBtn.title = "Inspect shortcuts (?)";
        helpBtn.addEventListener("click", () => showInspectOverlayShortcutHelp());
        toolbarActions.appendChild(helpBtn);

        const closeBtn = makeActionButton();
        closeBtn.textContent = "Close";
        closeBtn.style.cssText = actionButtonCss("subtle");
        closeBtn.addEventListener("click", () => closeInspectOverlay());
        toolbarActions.appendChild(closeBtn);

        toolbar.append(titleWrap, toolbarActions);

        const contentWrap = style(document.createElement("div"), `flex:1 1 auto;min-height:0;display:flex;gap:12px;`);
        const mediaWrap = style(document.createElement("div"), `flex:1 1 auto;min-width:0;min-height:0;display:flex;`);
        contentWrap.appendChild(mediaWrap);
        shell.append(toolbar, contentWrap);
        overlayEl.appendChild(shell);

        if (overlay.compareMode && compareCandidates.length >= 2) {
            ensureCompareDefaults(asset);
            renderCompareOverlay(asset, mediaWrap);
        } else {
            renderSingleOverlay(asset, mediaWrap);
        }
        if (overlay.showMetadata) {
            if (overlay.compareMode && compareCandidates.length >= 2) {
                const compareAssets = [
                    data.assets.find((entry) => entry.asset_id === overlay.compareLeftAssetId),
                    data.assets.find((entry) => entry.asset_id === overlay.compareRightAssetId),
                ].filter(Boolean);
                contentWrap.appendChild(renderOverlayMetadataPanel(compareAssets.length ? compareAssets : [asset]));
            } else {
                contentWrap.appendChild(renderOverlayMetadataPanel(asset));
            }
        }
    }

    function renderUsagesDetail(asset) {
        destroyLiveMedia();
        detailPane.innerHTML = "";

        if (!asset || state.showingUsagesFor !== asset.asset_id) {
            clearUsageView();
            renderDetail(asset);
            return;
        }

        const headerRow = style(document.createElement("div"), `display:flex;align-items:flex-start;justify-content:space-between;gap:8px;min-width:0;`);
        const headingWrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:2px;min-width:0;`);
        const title = style(document.createElement("div"), `color:${CHROME.text};font-size:11px;font-weight:700;`);
        title.textContent = "Where Used";
        const subtitle = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        subtitle.textContent = assetDisplayName(asset);
        headingWrap.append(title, subtitle);

        const backBtn = makeActionButton();
        backBtn.textContent = "Back";
        backBtn.style.cssText = actionButtonCss("subtle");
        backBtn.addEventListener("click", () => {
            clearUsageView();
            render();
        });
        headerRow.append(headingWrap, backBtn);
        detailPane.appendChild(headerRow);

        const pathLine = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        pathLine.textContent = asset.path || "-";
        detailPane.appendChild(pathLine);

        if (state.usageLoading) {
            const loading = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;line-height:1.45;`);
            loading.textContent = "Loading usage...";
            detailPane.appendChild(loading);
            queueResize();
            return;
        }

        if (state.usageError) {
            const error = style(document.createElement("div"), `color:#ffb18c;font-size:10px;line-height:1.45;`);
            error.textContent = state.usageError;
            detailPane.appendChild(error);
            queueResize();
            return;
        }

        const usage = state.usageData || { usages: [], usage_count: 0 };
        const counts = summarizeUsageTypes(usage.usages || []);
        const summary = style(document.createElement("div"), `display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;`);
        for (const [label, value] of [
            ["Clips", counts.clip],
            ["Audio", counts.audio_track],
            ["Guides", counts.guide_frame],
            ["Queue", counts.generation_job],
        ]) {
            summary.appendChild(makeMetaCell(label, String(value)));
        }
        detailPane.appendChild(summary);

        if (!(usage.usage_count > 0)) {
            const empty = style(document.createElement("div"), `color:#7f8b96;font-size:10px;line-height:1.45;`);
            empty.textContent = "No clip, guide, audio, or generation job references were found for this asset.";
            detailPane.appendChild(empty);
            queueResize();
            return;
        }

        for (const group of groupUsagesByScene(usage.usages || [])) {
            detailPane.appendChild(makeSectionTitle(group.sceneName));
            const list = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;`);
            for (const item of group.items) {
                const row = style(document.createElement("div"), `padding:8px;border-radius:6px;background:rgba(255,255,255,0.03);border:1px solid #343434;display:flex;flex-direction:column;gap:4px;`);
                const typeLine = style(document.createElement("div"), `color:#ececec;font-size:10px;font-weight:600;`);
                typeLine.textContent = usageTypeLabel(item.type);
                const posLine = style(document.createElement("div"), `color:#8ea0af;font-size:10px;line-height:1.4;`);
                posLine.textContent = usagePositionLabel(item);
                row.append(typeLine, posLine);
                list.appendChild(row);
            }
            detailPane.appendChild(list);
        }
        queueResize();
    }

    function renderMultiDetail(assetIds) {
        destroyLiveMedia();
        detailPane.innerHTML = "";

        const assets = assetIds
            .map((assetId) => data.assets.find((asset) => asset.asset_id === assetId))
            .filter(Boolean);
        if (assets.length <= 1) {
            renderDetail(selectedAsset());
            return;
        }

        const typeBreakdown = summarizeAssetTypes(assets);
        const missingCount = assets.filter((asset) => assetIsMissing(asset)).length;
        const trashedCount = assets.filter((asset) => isTrashed(asset)).length;
        const activeAssets = assets.filter((asset) => !isTrashed(asset));
        const trashedSelection = assets.filter((asset) => isTrashed(asset));

        const title = style(document.createElement("div"), `color:#ececec;font-size:11px;font-weight:700;`);
        title.textContent = `${assets.length} Assets Selected`;
        const subtitle = style(document.createElement("div"), `color:#8ea0af;font-size:10px;line-height:1.45;`);
        subtitle.textContent = "Bulk actions apply to the current selection across both fullscreen and dormant galleries.";

        const stats = style(document.createElement("div"), `display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;`);
        stats.append(
            makeMetaCell("Videos", String(typeBreakdown.video)),
            makeMetaCell("Images", String(typeBreakdown.image)),
            makeMetaCell("Audio", String(typeBreakdown.audio)),
            makeMetaCell("Missing", String(missingCount)),
            makeMetaCell("Trashed", String(trashedCount)),
            makeMetaCell("Total Duration", aggregateDurationLabel(assets)),
            makeMetaCell("Primary", selectedAsset() ? assetDisplayName(selectedAsset()) : "-"),
        );

        const actionRow = style(document.createElement("div"), `display:flex;flex-wrap:wrap;gap:6px;align-items:center;`);
        if (activeAssets.length) {
            const moveBtn = makeActionButton();
            moveBtn.textContent = activeAssets.length === assets.length ? "Move to Folder..." : `Move ${activeAssets.length} Active`;
            moveBtn.disabled = !options.onBulkMoveAssets;
            moveBtn.addEventListener("click", async (event) => {
                await handleBulkMove(activeAssets.map((asset) => asset.asset_id), event);
            });
            actionRow.appendChild(moveBtn);

            const deleteBtn = makeActionButton();
            deleteBtn.textContent = activeAssets.length === assets.length ? "Move to Trash" : `Trash ${activeAssets.length} Active`;
            deleteBtn.style.background = "#392420";
            deleteBtn.style.borderColor = "#73443b";
            deleteBtn.disabled = !options.onBulkDeleteAssets;
            deleteBtn.addEventListener("click", async () => {
                await handleBulkDelete(activeAssets.map((asset) => asset.asset_id));
            });
            actionRow.appendChild(deleteBtn);
        }

        if (trashedSelection.length) {
            const restoreBtn = makeActionButton();
            restoreBtn.textContent = trashedSelection.length === assets.length ? "Restore Selected" : `Restore ${trashedSelection.length} Trashed`;
            restoreBtn.style.background = "#203427";
            restoreBtn.style.borderColor = "#48644f";
            restoreBtn.disabled = !options.onBulkRestoreAssets;
            restoreBtn.addEventListener("click", async () => {
                await handleBulkRestore(trashedSelection.map((asset) => asset.asset_id));
            });
            actionRow.appendChild(restoreBtn);

            const permanentBtn = makeActionButton();
            permanentBtn.textContent = trashedSelection.length === assets.length ? "Delete Permanently" : `Delete ${trashedSelection.length} Permanently`;
            permanentBtn.style.background = "#4a1c1c";
            permanentBtn.style.borderColor = "#8a3f3f";
            permanentBtn.disabled = !options.onBulkPermanentDeleteAssets;
            permanentBtn.addEventListener("click", async () => {
                await handleBulkPermanentDelete(trashedSelection.map((asset) => asset.asset_id));
            });
            actionRow.appendChild(permanentBtn);
        }

        detailPane.append(title, subtitle, stats, actionRow);
        queueResize();
    }

    function renderDetail(asset) {
        const selectedIds = selectedAssetIdsList();
        if (selectedIds.length > 1) {
            renderMultiDetail(selectedIds);
            return;
        }

        if (state.showingUsagesFor) {
            const usageAsset = asset?.asset_id === state.showingUsagesFor
                ? asset
                : data.assets.find((entry) => entry.asset_id === state.showingUsagesFor);
            if (usageAsset) {
                renderUsagesDetail(usageAsset);
                return;
            }
            clearUsageView();
        }

        destroyLiveMedia();
        detailPane.innerHTML = "";
        if (!asset) {
            const emptyTitle = style(document.createElement("div"), `color:${CHROME.text};font-size:11px;font-weight:700;`);
            emptyTitle.textContent = "Asset Details";
            const emptyText = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;line-height:1.45;`);
            emptyText.textContent = "Select an asset to inspect it. Right-click the gallery background to create folders or import files.";
            detailPane.append(emptyTitle, emptyText);
            queueResize();
            return;
        }

        const titleRow = style(document.createElement("div"), `display:flex;align-items:center;justify-content:space-between;gap:8px;min-width:0;`);
        const title = style(document.createElement("div"), `color:${CHROME.text};font-size:11px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        title.textContent = assetDisplayName(asset);
        const badges = style(document.createElement("div"), `display:flex;align-items:center;gap:6px;flex:0 0 auto;`);
        const kind = style(document.createElement("div"), `padding:3px 6px;border-radius:999px;background:rgba(143,192,240,0.12);border:1px solid rgba(143,192,240,0.28);color:#9fc8ea;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;white-space:nowrap;`);
        kind.textContent = assetKindLabel(asset.asset_type);
        badges.appendChild(kind);
        if (asset.asset_type === "artifact") {
            const artifactBadge = style(document.createElement("div"), `padding:3px 6px;border-radius:999px;background:rgba(181,199,116,0.12);border:1px solid rgba(181,199,116,0.3);color:#d7e59f;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;white-space:nowrap;`);
            artifactBadge.textContent = String(asset.artifact_kind || "other");
            badges.appendChild(artifactBadge);
        }
        const workflowFlag = embeddedWorkflowFlag(asset);
        if (workflowFlag !== null) {
            const workflowBadge = style(document.createElement("div"), `padding:3px 6px;border-radius:999px;background:${workflowFlag ? "rgba(120,172,124,0.14)" : "rgba(160,150,126,0.12)"};border:1px solid ${workflowFlag ? "rgba(120,172,124,0.36)" : "rgba(177,166,139,0.3)"};color:${workflowFlag ? "#b7e4b4" : "#cfc4a5"};font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;white-space:nowrap;`);
            workflowBadge.textContent = workflowFlag ? "Workflow" : "No Workflow";
            badges.appendChild(workflowBadge);
        }
        if (isTrashed(asset)) {
            const trashedBadge = style(document.createElement("div"), `padding:3px 6px;border-radius:999px;background:rgba(184,96,72,0.18);border:1px solid rgba(214,132,98,0.45);color:#f0b39f;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;white-space:nowrap;`);
            trashedBadge.textContent = "Trashed";
            badges.appendChild(trashedBadge);
        }
        if (assetIsMissing(asset)) {
            const missingBadge = style(document.createElement("div"), `padding:3px 6px;border-radius:999px;background:rgba(204,92,48,0.14);border:1px solid rgba(255,158,112,0.35);color:#ffb18c;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;white-space:nowrap;`);
            missingBadge.textContent = "Missing";
            badges.appendChild(missingBadge);
        }
        titleRow.append(title, badges);
        const pathLine = style(document.createElement("div"), `color:${assetIsMissing(asset) ? "#ffb18c" : "#7f8b96"};font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        pathLine.textContent = asset.path || "-";

        const previewSurface = style(document.createElement("div"), `min-height:140px;border-radius:8px;border:1px solid ${CHROME.border};background:${CHROME.panelMuted};overflow:hidden;display:flex;align-items:center;justify-content:center;position:relative;`);
        const previewExtras = [];
        const projectDir = currentProjectDir();
        if (assetIsMissing(asset)) {
            const missingWrap = style(document.createElement("div"), `padding:18px;text-align:center;display:flex;flex-direction:column;gap:6px;align-items:center;`);
            const missingTitle = style(document.createElement("div"), `color:#ffb18c;font-size:11px;font-weight:700;`);
            missingTitle.textContent = "Missing Asset";
            const missingText = style(document.createElement("div"), `color:#d4a690;font-size:10px;line-height:1.45;max-width:280px;`);
            missingText.textContent = "The source file is gone, but this asset entry still exists. Relink it to restore existing references, or delete the asset to remove it from the project.";
            missingWrap.append(missingTitle, missingText);
            previewSurface.appendChild(missingWrap);
        } else if (asset.asset_type === "image") {
            const img = style(document.createElement("img"), `width:100%;max-height:220px;object-fit:contain;display:block;`);
            img.src = buildAssetViewUrl(projectDir, asset.path);
            img.alt = assetDisplayName(asset);
            previewSurface.appendChild(img);
        } else if (asset.asset_type === "video") {
            const video = style(document.createElement("video"), `width:100%;max-height:220px;display:block;background:#000;`);
            video.controls = true;
            video.preload = "metadata";
            video.playsInline = true;
            if (asset.has_thumbnail) video.poster = buildThumbnailUrl(projectDir, asset.asset_id);
            const blobHandle = loadMediaAsBlob(buildAssetViewUrl(projectDir, asset.path), video);
            previewSurface.appendChild(video);
            state.liveMedia = video;
            const scrubBar = renderMediaScrubBar(video);
            const scrubCleanup = scrubBar.cleanup;
            state.liveMediaCleanup = () => { scrubCleanup(); blobHandle.cleanup(); };
            previewExtras.push(scrubBar.el);
        } else if (asset.asset_type === "audio") {
            const audioWrap = style(document.createElement("div"), `width:100%;padding:16px;display:flex;flex-direction:column;gap:12px;align-items:stretch;box-sizing:border-box;`);
            const audioLabel = style(document.createElement("div"), `color:#9fb6c8;font-size:10px;text-transform:uppercase;letter-spacing:0.08em;`);
            audioLabel.textContent = "Audio Preview";
            const audio = style(document.createElement("audio"), `width:100%;display:block;`);
            audio.controls = true;
            audio.preload = "metadata";
            const blobHandle = loadMediaAsBlob(buildAssetViewUrl(projectDir, asset.path), audio);
            audioWrap.append(audioLabel, audio);
            previewSurface.appendChild(audioWrap);
            state.liveMedia = audio;
            const scrubBar = renderMediaScrubBar(audio);
            const scrubCleanup = scrubBar.cleanup;
            state.liveMediaCleanup = () => { scrubCleanup(); blobHandle.cleanup(); };
            previewExtras.push(scrubBar.el);
        } else if (asset.asset_type === "artifact") {
            const artifactWrap = style(document.createElement("div"), `width:100%;padding:16px;display:flex;flex-direction:column;gap:10px;align-items:flex-start;box-sizing:border-box;`);
            const artifactLabel = style(document.createElement("div"), `color:#d7e59f;font-size:10px;text-transform:uppercase;letter-spacing:0.08em;`);
            artifactLabel.textContent = "Artifact Metadata";
            const artifactIntro = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;line-height:1.5;max-width:340px;`);
            artifactIntro.textContent = "Artifacts stay inspectable and searchable, but they do not render a media preview in the gallery.";
            const artifactSummary = style(document.createElement("div"), `display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;width:100%;`);
            artifactSummary.appendChild(makeMetaCell("Kind", String(asset.artifact_kind || "other")));
            artifactSummary.appendChild(makeMetaCell("Ext", assetExtension(asset) ? `.${assetExtension(asset)}` : "-"));
            artifactSummary.appendChild(makeMetaCell("Size", formatBytes(asset.size_bytes)));
            artifactSummary.appendChild(makeMetaCell("Imported", formatDate(asset.imported_at)));
            artifactWrap.append(artifactLabel, artifactIntro, artifactSummary);
            previewSurface.appendChild(artifactWrap);
        } else {
            const placeholder = style(document.createElement("div"), `color:#8ea0af;font-size:10px;`);
            placeholder.textContent = "Preview unavailable.";
            previewSurface.appendChild(placeholder);
        }

        const meta = style(document.createElement("div"), `display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;font-size:10px;color:#b9c3cb;`);
        const rows = asset.asset_type === "artifact"
            ? [
                ["Type", assetKindLabel(asset.asset_type)],
                ["Kind", String(asset.artifact_kind || "other")],
                ["Extension", assetExtension(asset) ? `.${assetExtension(asset)}` : "-"],
                ["Status", assetIsMissing(asset) ? "Missing" : "Available"],
                ["Folder", normalizeFolderName(asset.folder) || "Root"],
                ["Size", formatBytes(asset.size_bytes)],
                ["Source Workflow", workflowStatusLabel(asset)],
                ["Imported", formatDate(asset.imported_at)],
            ]
            : [
                ["Type", assetKindLabel(asset.asset_type)],
                ["Status", assetIsMissing(asset) ? "Missing" : "Available"],
                ["Folder", normalizeFolderName(asset.folder) || "Root"],
                ["Size", formatResolution(asset)],
                ["Duration", formatDuration(asset)],
                ["FPS", asset.fps ? String(asset.fps) : "-"],
                ["Sample Rate", asset.sample_rate ? `${asset.sample_rate} Hz` : "-"],
                ["Embedded Audio", asset.has_audio ? "Yes" : "No"],
                ["Source Workflow", workflowStatusLabel(asset)],
                ["Imported", formatDate(asset.imported_at)],
            ];
        if (isTrashed(asset)) {
            rows.push(["Trashed At", formatDate(asset.trashed_at)]);
            rows.push(["Restore To", normalizeFolderName(asset.trash_previous_folder) || "Root"]);
        }
        for (const [label, value] of rows) meta.appendChild(makeMetaCell(label, value));

        const detailSections = [titleRow, pathLine, previewSurface, ...previewExtras, meta];
        if (asset.asset_type === "artifact") {
            const artifactToggle = makeActionButton(state.artifactInspectorExpanded ? "active" : "subtle");
            artifactToggle.textContent = state.artifactInspectorExpanded ? "Collapse Metadata" : "Expand Metadata";
            artifactToggle.addEventListener("click", () => {
                state.artifactInspectorExpanded = !state.artifactInspectorExpanded;
                persistArtifactInspectorExpanded();
                renderDetail(asset);
            });
            detailSections.push(artifactToggle);
            if (state.artifactInspectorExpanded) {
                const tracked = renderTrackedMetadataSection(asset);
                if (tracked) detailSections.push(tracked);
                detailSections.push(renderGenerationSection(asset));
            }
        } else {
            const tracked = renderTrackedMetadataSection(asset);
            if (tracked) detailSections.push(tracked);
            detailSections.push(renderGenerationSection(asset));
        }
        if (isTrashed(asset)) {
            const trashedNote = style(document.createElement("div"), `padding:8px;border-radius:6px;background:rgba(184,96,72,0.12);border:1px solid rgba(214,132,98,0.3);color:#d8b4aa;font-size:10px;line-height:1.45;`);
            trashedNote.textContent = "Trashed assets stay out of the normal gallery but keep their references recoverable until they are permanently deleted.";
            const trashActions = style(document.createElement("div"), `display:flex;flex-wrap:wrap;gap:6px;align-items:center;`);
            const restoreBtn = makeActionButton();
            restoreBtn.textContent = "Restore";
            restoreBtn.style.background = "#203427";
            restoreBtn.style.borderColor = "#48644f";
            restoreBtn.disabled = !options.onRestoreAsset;
            restoreBtn.addEventListener("click", async () => {
                await handleAssetRestore(asset);
            });
            trashActions.appendChild(restoreBtn);

            const permanentBtn = makeActionButton();
            permanentBtn.textContent = "Delete Permanently";
            permanentBtn.style.background = "#4a1c1c";
            permanentBtn.style.borderColor = "#8a3f3f";
            permanentBtn.disabled = !options.onPermanentDeleteAsset;
            permanentBtn.addEventListener("click", async () => {
                await handleAssetPermanentDelete(asset);
            });
            trashActions.appendChild(permanentBtn);
            detailSections.push(trashedNote, trashActions);
        } else {
            const form = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;`);
            const nameLabel = makeSectionTitle("Display Name");
            const nameInput = style(document.createElement("input"), inputChromeCss());
            nameInput.value = asset.name || "";
            const renameHint = style(document.createElement("div"), `color:${CHROME.textDim};font-size:10px;line-height:1.35;`);
            renameHint.textContent = "This only changes the display name. The file path and extension on disk stay unchanged.";
            const folderLabel = makeSectionTitle("Folder");
            const folderInput = style(document.createElement("input"), inputChromeCss());
            folderInput.value = normalizeFolderName(asset.folder);
            folderInput.placeholder = "Root";
            folderInput.setAttribute("list", folderListId);
            const saveBtn = makeActionButton();
            saveBtn.textContent = "Save Metadata";
            const actionRow = style(document.createElement("div"), `display:flex;flex-wrap:wrap;gap:6px;align-items:center;`);
            saveBtn.addEventListener("click", async () => {
                try {
                    setBusyButton(saveBtn, true, "Saving...", "Save Metadata");
                    await applyAssetUpdate(asset, { name: nameInput.value, folder: folderInput.value });
                } catch (error) {
                    console.warn("[Sonder] Failed to save asset metadata:", error);
                } finally {
                    setBusyButton(saveBtn, false, "Saving...", "Save Metadata");
                }
            });
            actionRow.appendChild(saveBtn);

            const replaceBtn = makeActionButton();
            replaceBtn.textContent = assetIsMissing(asset) ? "Relink" : "Replace File";
            replaceBtn.style.cssText = actionButtonCss("subtle");
            replaceBtn.addEventListener("click", async () => {
                await beginAssetReplace(asset);
            });
            actionRow.appendChild(replaceBtn);

            const deleteBtn = makeActionButton();
            deleteBtn.textContent = "Move to Trash";
            deleteBtn.style.cssText = actionButtonCss("danger");
            deleteBtn.addEventListener("click", async () => {
                await handleAssetDelete(asset);
            });
            actionRow.appendChild(deleteBtn);

            form.append(nameLabel, nameInput, renameHint, folderLabel, folderInput, actionRow);
            detailSections.push(form);
        }

        detailPane.append(...detailSections);
        queueResize();
    }

    async function promptCreateFolder(parentFolder = "") {
        const promptLabel = parentFolder ? `New folder name under "${parentFolder}":` : "New folder name:";
        const folderName = prompt(promptLabel);
        if (folderName === null || !folderName.trim()) return;
        const normalized = normalizeFolderName(parentFolder ? `${parentFolder}/${folderName.trim()}` : folderName.trim());
        if (!normalized) return;
        try {
            const nextFolders = await options.onCreateFolder?.(normalized);
            if (Array.isArray(nextFolders)) {
                data.folders = nextFolders.map(normalizeFolderName).filter(Boolean);
            } else if (!data.folders.includes(normalized)) {
                data.folders = [...data.folders, normalized].sort(compareStrings);
            }
            updateFolderOptions();
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to create folder:", error);
        }
    }

    async function handleAssetRename(asset) {
        const nextName = prompt("Display name:", asset.name || assetDisplayName(asset));
        if (nextName === null) return;
        await applyAssetUpdate(asset, { name: nextName.trim() });
    }

    function showFolderPicker(event, currentFolder, onSelect) {
        const folders = ["", ...data.folders.filter(Boolean)];
        const items = folders.map((folder) => ({
            label: folder || "Root",
            action: () => onSelect(normalizeFolderName(folder)),
            disabled: normalizeFolderName(folder) === normalizeFolderName(currentFolder),
        }));
        items.push({ type: "separator" });
        items.push({
            label: "New Folder...",
            action: async () => {
                const name = prompt("New folder name:");
                if (!name) return;
                const normalized = normalizeFolderName(name);
                if (normalized) onSelect(normalized);
            },
        });
        const rect = event?.currentTarget?.getBoundingClientRect?.();
        const x = rect ? rect.left : (event?.clientX ?? 100);
        const y = rect ? rect.bottom + 2 : (event?.clientY ?? 100);
        showContextMenu(x, y, items);
    }

    async function handleAssetMoveToFolder(asset, event) {
        showFolderPicker(event, asset.folder, async (folder) => {
            await applyAssetUpdate(asset, { folder });
        });
    }

    async function beginAssetReplace(asset) {
        if (!asset?.asset_id || !options.onReplaceAsset) return;
        state.replaceAssetId = asset.asset_id;
        replaceInput.value = "";
        replaceInput.click();
    }

    async function handleAssetDelete(asset) {
        if (!asset?.asset_id || !options.onDeleteAsset) return;
        try {
            const message = `Move "${assetDisplayName(asset)}" to Trash? You can restore it later until the trash is emptied.`;
            if (!confirm(message)) return;

            const result = await options.onDeleteAsset(asset.asset_id, false);
            if (result?.status === "conflict") {
                throw new Error("Asset trash unexpectedly reported a conflict.");
            }

            updateAsset({
                ...asset,
                folder: "",
                trashed_at: result?.trashed_at || new Date().toISOString(),
                trash_previous_folder: asset.trash_previous_folder || normalizeFolderName(asset.folder),
            });
            clearUsageView();
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to trash asset:", error);
            alert(error?.message || "Failed to move asset to Trash.");
        }
    }

    async function promptRenameFolder(folderName) {
        if (!folderName || !options.onRenameFolder) return;
        const leafName = normalizeFolderName(folderName).split("/").pop() || folderName;
        const nextLeaf = prompt("Rename folder:", leafName);
        if (nextLeaf === null || !nextLeaf.trim()) return;
        const parentParts = normalizeFolderName(folderName).split("/").slice(0, -1);
        const nextFolder = normalizeFolderName([...parentParts, nextLeaf.trim()].join("/"));
        if (!nextFolder || nextFolder === normalizeFolderName(folderName)) return;
        try {
            const payload = await options.onRenameFolder(folderName, nextFolder);
            renameFolderLocally(folderName, nextFolder, Array.isArray(payload) ? payload : payload?.folders);
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to rename folder:", error);
            alert(error?.message || "Failed to rename folder.");
        }
    }

    async function handleFolderDelete(folderName) {
        if (!folderName || !options.onDeleteFolder) return;
        try {
            const containedAssets = folderAssetsRecursive(folderName);
            const message = containedAssets.length > 0
                ? `Move folder "${folderName}" and its ${containedAssets.length} asset(s) to Trash? You can restore the assets later from Trash.`
                : `Delete empty folder "${folderName}"?`;
            if (!confirm(message)) return;

            const result = await options.onDeleteFolder(folderName, false);
            if (result?.status === "conflict") {
                throw new Error("Folder trash unexpectedly reported a conflict.");
            }

            for (const asset of containedAssets) {
                updateAsset({
                    ...asset,
                    folder: "",
                    trashed_at: new Date().toISOString(),
                    trash_previous_folder: asset.trash_previous_folder || normalizeFolderName(asset.folder),
                });
            }
            removeFolderLocally(folderName);
            clearUsageView();
            render();
        } catch (error) {
            console.warn("[Sonder] Failed to trash folder:", error);
            alert(error?.message || "Failed to move folder to Trash.");
        }
    }

    function backgroundContextMenuItems() {
        return [
            { label: "Create Folder", action: async () => await promptCreateFolder() },
            { label: "Import Files", action: () => fileInput.click() },
            { label: "Refresh", action: async () => await options.onRefresh?.() },
        ];
    }

    function folderContextMenuItems(folderName) {
        const normalized = normalizeFolderName(folderName);
        if (normalized === TRASH_FOLDER_COLLAPSE_KEY) {
            const trashCount = trashedAssets().length;
            return [
                { label: isFolderCollapsed(TRASH_FOLDER_COLLAPSE_KEY) ? "Expand Trash" : "Collapse Trash", action: () => toggleFolderCollapsed(TRASH_FOLDER_COLLAPSE_KEY) },
                { type: "separator" },
                { label: `Restore All (${trashCount})`, disabled: !trashCount || !options.onBulkRestoreAssets, action: async () => await handleBulkRestore(trashedAssets().map((asset) => asset.asset_id)) },
                { label: "Empty Trash", disabled: !trashCount || !options.onEmptyTrash, danger: true, action: async () => await handleEmptyTrash() },
            ];
        }
        if (!normalized) {
            return [
                { label: isFolderCollapsed("") ? "Expand Root" : "Collapse Root", action: () => toggleFolderCollapsed("") },
                { type: "separator" },
                ...backgroundContextMenuItems(),
            ];
        }
        return [
            { label: isFolderCollapsed(normalized) ? "Expand Folder" : "Collapse Folder", action: () => toggleFolderCollapsed(normalized) },
            { label: "Create Subfolder", action: async () => await promptCreateFolder(normalized) },
            { type: "separator" },
            { label: "Rename Folder", disabled: !options.onRenameFolder, action: async () => await promptRenameFolder(normalized) },
            { label: "Delete Folder", disabled: !options.onDeleteFolder, action: async () => await handleFolderDelete(normalized) },
        ];
    }

    function assetContextMenuItems(asset) {
        const selectedIds = selectedAssetIdsList();
        if (selectedIds.length > 1 && state.selectedAssetIds.has(asset.asset_id)) {
            const selectionAssets = data.assets.filter((entry) => selectedIds.includes(entry.asset_id));
            const activeIds = selectionAssets.filter((entry) => !isTrashed(entry)).map((entry) => entry.asset_id);
            const trashedIds = selectionAssets.filter((entry) => isTrashed(entry)).map((entry) => entry.asset_id);
            return [
                ...(activeIds.length ? [{
                    label: `Move ${activeIds.length} asset${activeIds.length === 1 ? "" : "s"} to folder...`,
                    disabled: !options.onBulkMoveAssets,
                    action: async () => await handleBulkMove(activeIds),
                }] : []),
                ...(activeIds.length ? [{
                    label: `Trash ${activeIds.length} asset${activeIds.length === 1 ? "" : "s"}`,
                    disabled: !options.onBulkDeleteAssets,
                    danger: true,
                    action: async () => await handleBulkDelete(activeIds),
                }] : []),
                ...((activeIds.length && trashedIds.length) ? [{ type: "separator" }] : []),
                ...(trashedIds.length ? [{
                    label: `Restore ${trashedIds.length} asset${trashedIds.length === 1 ? "" : "s"}`,
                    disabled: !options.onBulkRestoreAssets,
                    action: async () => await handleBulkRestore(trashedIds),
                }] : []),
                ...(trashedIds.length ? [{
                    label: `Delete ${trashedIds.length} permanently`,
                    disabled: !options.onBulkPermanentDeleteAssets,
                    danger: true,
                    action: async () => await handleBulkPermanentDelete(trashedIds),
                }] : []),
            ];
        }

        if (isTrashed(asset)) {
            return [
                {
                    label: "Restore",
                    disabled: !options.onRestoreAsset,
                    action: async () => {
                        selectAsset(asset.asset_id, { focusList: true });
                        await handleAssetRestore(asset);
                    },
                },
                {
                    label: "Delete Permanently",
                    disabled: !options.onPermanentDeleteAsset,
                    danger: true,
                    action: async () => {
                        selectAsset(asset.asset_id, { focusList: true });
                        await handleAssetPermanentDelete(asset);
                    },
                },
            ];
        }

        return [
            {
                label: "Inspect Asset",
                action: () => {
                    selectAsset(asset.asset_id, { focusList: true });
                    openInspectOverlay(asset);
                },
            },
            {
                label: "Open Source Workflow",
                disabled: !options.onOpenSourceWorkflow || !canOpenWorkflowFor(asset),
                action: async () => {
                    selectAsset(asset.asset_id, { focusList: true });
                    await options.onOpenSourceWorkflow?.(asset);
                },
            },
            ...((asset.width || 0) > 0 && (asset.height || 0) > 0 ? [
                {
                    label: "Set Scene Aspect Ratio",
                    disabled: !options.onSetSceneAspectRatio,
                    action: () => {
                        selectAsset(asset.asset_id, { focusList: true });
                        options.onSetSceneAspectRatio?.(asset.width, asset.height);
                    },
                },
            ] : []),
            { type: "separator" },
            {
                label: "Rename",
                action: async () => {
                    selectAsset(asset.asset_id, { focusList: true });
                    await handleAssetRename(asset);
                },
            },
            {
                label: "Move to Folder...",
                action: async () => {
                    selectAsset(asset.asset_id, { focusList: true });
                    await handleAssetMoveToFolder(asset);
                },
            },
            {
                label: "Where Used...",
                disabled: !options.onGetAssetUsages,
                action: async () => {
                    selectAsset(asset.asset_id, { focusList: true });
                    await openUsageView(asset);
                },
            },
            {
                label: assetIsMissing(asset) ? "Relink..." : "Replace File...",
                disabled: !options.onReplaceAsset,
                action: async () => {
                    selectAsset(asset.asset_id, { focusList: true });
                    await beginAssetReplace(asset);
                },
            },
            { type: "separator" },
            { label: "Move to Trash", disabled: !options.onDeleteAsset, danger: true, action: async () => await handleAssetDelete(asset) },
        ];
    }

    function renderFolderHeader(folderName, assetCount) {
        const normalized = normalizeFolderName(folderName);
        const collapsed = isFolderCollapsed(normalized);
        const header = style(document.createElement("div"), `display:flex;align-items:center;justify-content:space-between;gap:8px;padding:5px 8px;border-radius:6px;background:${normalized ? "#1a1a2e" : "#1f2530"};color:#8fc0f0;font-size:10px;font-weight:600;border:1px solid transparent;`);
        header.dataset.folderDrop = normalized;
        header.dataset.folderHeader = normalized || "__root__";
        header.title = state.manageMode
            ? (normalized ? "Drop selected assets here to move them into this folder." : "Drop selected assets here to move them to Root.")
            : (normalized ? "Drop files here to import into this folder." : "Root folder");

        const left = style(document.createElement("div"), `display:flex;align-items:center;gap:6px;min-width:0;`);
        const toggle = style(document.createElement("button"), `width:18px;height:18px;border-radius:4px;border:1px solid #34414d;background:#182330;color:#9fc8ea;cursor:pointer;font-size:10px;line-height:1;padding:0;flex:0 0 auto;`);
        toggle.type = "button";
        toggle.textContent = collapsed ? ">" : "v";
        toggle.title = collapsed ? `Expand ${normalized || "root"}` : `Collapse ${normalized || "root"}`;
        toggle.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            toggleFolderCollapsed(normalized);
        });
        left.appendChild(toggle);

        const label = style(document.createElement("div"), `overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:${normalized ? "pointer" : "default"};`);
        label.textContent = normalized || "Root";
        label.style.cursor = "pointer";
        label.addEventListener("click", (event) => {
            event.preventDefault();
            toggleFolderCollapsed(normalized);
        });
        left.appendChild(label);

        const count = style(document.createElement("div"), `color:#9fc8ea;font-size:10px;flex:0 0 auto;`);
        count.textContent = String(assetCount);
        header.append(left, count);
        header.addEventListener("contextmenu", (event) => {
            if (event.target.closest("input, textarea, button, select, option")) return;
            event.preventDefault();
            event.stopPropagation();
            showContextMenu(event.clientX, event.clientY, folderContextMenuItems(normalized));
        });
        header.addEventListener("dragover", (event) => {
            if (!options.onBulkMoveAssets || !dataTransferHasType(event.dataTransfer, "application/x-sonder-asset-move")) return;
            event.preventDefault();
            event.stopPropagation();
            event.dataTransfer.dropEffect = "move";
            root.style.outline = "none";
            setDropFolderHighlight(normalized);
        });
        header.addEventListener("dragleave", (event) => {
            if (event.currentTarget !== event.target) return;
            if (state.dropFolder === normalized) clearDropFolderHighlight();
        });
        header.addEventListener("drop", async (event) => {
            if (!options.onBulkMoveAssets || !dataTransferHasType(event.dataTransfer, "application/x-sonder-asset-move")) return;
            event.preventDefault();
            event.stopPropagation();
            root.style.outline = "none";
            clearDropFolderHighlight();
            hideContextMenu();
            try {
                const rawPayload = event.dataTransfer.getData("application/x-sonder-asset-move");
                const payload = rawPayload ? JSON.parse(rawPayload) : {};
                const assetIds = Array.isArray(payload?.assetIds) ? payload.assetIds : [];
                const primaryAssetId = typeof payload?.primaryAssetId === "string" ? payload.primaryAssetId : (assetIds[assetIds.length - 1] || "");
                if (!assetIds.length) return;
                await options.onBulkMoveAssets(assetIds, normalized);
                applySelectionState(assetIds, primaryAssetId);
                clearUsageView();
                await options.onRefresh?.();
            } catch (error) {
                console.warn("[Sonder] Failed to drop-move assets:", error);
                alert(error?.message || "Failed to move selected assets.");
            }
        });
        return header;
    }

    function renderTrashFolderHeader(assetCount) {
        const collapsed = isFolderCollapsed(TRASH_FOLDER_COLLAPSE_KEY);
        const header = style(document.createElement("div"), `display:flex;align-items:center;justify-content:space-between;gap:8px;padding:5px 8px;border-radius:6px;background:#2a1814;color:#f0b39f;font-size:10px;font-weight:600;border:1px solid rgba(214,132,98,0.22);`);
        header.dataset.folderHeader = TRASH_FOLDER_COLLAPSE_KEY;

        const left = style(document.createElement("div"), `display:flex;align-items:center;gap:6px;min-width:0;`);
        const toggle = style(document.createElement("button"), `width:18px;height:18px;border-radius:4px;border:1px solid #73443b;background:#3a211d;color:#f3c9bb;cursor:pointer;font-size:10px;line-height:1;padding:0;flex:0 0 auto;`);
        toggle.type = "button";
        toggle.textContent = collapsed ? ">" : "v";
        toggle.addEventListener("click", (event) => {
            event.preventDefault();
            event.stopPropagation();
            toggleFolderCollapsed(TRASH_FOLDER_COLLAPSE_KEY);
        });
        const label = style(document.createElement("div"), `overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;`);
        label.textContent = "Trash";
        label.addEventListener("click", (event) => {
            event.preventDefault();
            toggleFolderCollapsed(TRASH_FOLDER_COLLAPSE_KEY);
        });
        left.append(toggle, label);

        const count = style(document.createElement("div"), `color:#f0b39f;font-size:10px;flex:0 0 auto;`);
        count.textContent = String(assetCount);
        header.append(left, count);
        header.addEventListener("contextmenu", (event) => {
            event.preventDefault();
            event.stopPropagation();
            showContextMenu(event.clientX, event.clientY, folderContextMenuItems(TRASH_FOLDER_COLLAPSE_KEY));
        });
        return header;
    }

    async function handleFileInputChange() {
        const files = Array.from(fileInput.files || []);
        if (!files.length) return;
        try {
            await options.onImportFiles?.(files, "");
            await options.onRefresh?.();
        } finally {
            fileInput.value = "";
        }
    }

    async function handleReplaceInputChange() {
        const file = replaceInput.files?.[0];
        const assetId = state.replaceAssetId;
        state.replaceAssetId = "";
        if (!file || !assetId || !options.onReplaceAsset) {
            replaceInput.value = "";
            return;
        }
        try {
            await options.onReplaceAsset(assetId, file);
            clearUsageView();
            await options.onRefresh?.();
        } catch (error) {
            console.warn("[Sonder] Failed to replace asset:", error);
        } finally {
            replaceInput.value = "";
        }
    }

    function render() {
        if (state.destroyed) return;
        ensureProjectPrefs();
        updateControlState();
        updateLayout();
        updateFolderOptions();
        renderBulkToolbar();
        tabsRow.innerHTML = "";
        listScroller.innerHTML = "";

        const activeAssets = data.assets.filter((asset) => !isTrashed(asset));
        const counts = {
            all: activeAssets.length,
            video: activeAssets.filter((asset) => asset.asset_type === "video").length,
            image: activeAssets.filter((asset) => asset.asset_type === "image").length,
            audio: activeAssets.filter((asset) => asset.asset_type === "audio").length,
            artifact: activeAssets.filter((asset) => asset.asset_type === "artifact").length,
        };
        const tabs = [
            ["all", `All (${counts.all})`],
            ["video", `Videos (${counts.video})`],
            ["image", `Images (${counts.image})`],
            ["audio", `Audio (${counts.audio})`],
            ["artifact", `Artifacts (${counts.artifact})`],
        ];
        for (const [type, label] of tabs) {
            const isActive = state.type === type;
            const tab = style(document.createElement("button"), `${actionButtonCss(isActive ? "active" : "subtle")} padding:5px 8px;font-size:10px;`);
            tab.textContent = label;
            tab.addEventListener("click", () => {
                state.type = type;
                state.allowAutoFocus = true;
                render();
            });
            tabsRow.appendChild(tab);
        }

        const assets = filteredAssets();
        const visibleAssets = navigableAssets();
        const thumbConfig = thumbnailSizeConfig(state.thumbnailSize);
        ensureFocusedAsset(visibleAssets);
        const selected = selectedAsset();
        const folders = allFolders();
        let renderedAnything = false;

        for (const folderName of folders) {
            if (isAncestorCollapsed(folderName)) continue;

            const inFolder = folderAssets(folderName, assets);
            renderedAnything = true;
            listScroller.appendChild(renderFolderHeader(folderName, inFolder.length));

            if (isFolderCollapsed(folderName)) continue;

            if (!inFolder.length) {
                const emptyFolder = style(document.createElement("div"), `padding:8px 10px;border-radius:6px;border:1px dashed ${CHROME.borderStrong};color:${CHROME.textDim};font-size:10px;margin-bottom:2px;background:rgba(255,255,255,0.02);`);
                emptyFolder.textContent = folderName ? "Empty folder. Drop files here to import into it." : "No root assets match the current filter.";
                listScroller.appendChild(emptyFolder);
                continue;
            }

            for (const asset of inFolder) {
                const isSelected = state.selectedAssetIds.has(asset.asset_id);
                const isPrimary = state.selectedAssetId === asset.asset_id;
                const isFocused = state.focusedAssetId === asset.asset_id;
                const isMissing = assetIsMissing(asset);
                const borderColor = isSelected
                    ? (isMissing ? "#c97a59" : (isPrimary ? "#8fbbe5" : "#6f8ea8"))
                    : (isMissing ? "#8d5c4b" : (isFocused ? "#7f8b96" : "#373737"));
                const background = isSelected
                    ? (isMissing ? "rgba(133,82,58,0.24)" : "rgba(75,105,135,0.18)")
                    : (isMissing ? "rgba(96,54,39,0.18)" : (isFocused ? "rgba(255,255,255,0.05)" : "rgba(255,255,255,0.03)"));
                const focusRing = isPrimary
                    ? "box-shadow:inset 0 0 0 1px rgba(143,192,240,0.45);"
                    : (isFocused ? "box-shadow:inset 0 0 0 1px rgba(143,192,240,0.25);" : "");
                const row = style(document.createElement("div"), `display:grid;grid-template-columns:${state.manageMode ? `24px ${thumbConfig.thumbWidth}px minmax(0,1fr)` : `${thumbConfig.thumbWidth}px minmax(0,1fr)`};gap:${thumbConfig.gap}px;padding:${thumbConfig.padding}px;border-radius:6px;border:1px solid ${borderColor};background:${background};cursor:pointer;${focusRing}`);
                row.dataset.assetRow = asset.asset_id;
                row.draggable = state.manageMode ? !!options.onBulkMoveAssets : true;
                row.title = state.manageMode
                    ? "Click to inspect. Drag onto a folder header to move assets."
                    : (asset.asset_type === "artifact"
                        ? "Click to inspect. Artifact graph-drop support is deferred."
                        : "Click to inspect. Drag onto the graph to create a loader node.");
                row.addEventListener("click", (event) => {
                    handleAssetActivation(asset.asset_id, event, visibleAssets, { focusList: true, scrollIntoView: true });
                });
                row.addEventListener("dblclick", () => {
                    openInspectOverlay(asset);
                });
                row.addEventListener("contextmenu", (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    if (!(state.selectedAssetIds.has(asset.asset_id) && selectedAssetIdsList().length > 1)) {
                        selectAsset(asset.asset_id, { focusList: true });
                    }
                    showContextMenu(event.clientX, event.clientY, assetContextMenuItems(asset));
                });
                row.addEventListener("dragstart", (event) => {
                    if (state.manageMode) {
                        if (!options.onBulkMoveAssets) {
                            event.preventDefault();
                            return;
                        }
                        const moveIds = state.selectedAssetIds.has(asset.asset_id)
                            ? selectedAssetIdsList()
                            : [asset.asset_id];
                        event.dataTransfer.effectAllowed = "move";
                        event.dataTransfer.setData("application/x-sonder-asset-move", JSON.stringify({
                            assetIds: moveIds,
                            primaryAssetId: state.selectedAssetIds.has(asset.asset_id)
                                ? (state.selectedAssetId || asset.asset_id)
                                : asset.asset_id,
                        }));
                        event.dataTransfer.setData("text/plain", moveIds.length > 1 ? `${moveIds.length} assets` : assetDisplayName(asset));
                        return;
                    }
                    const payload = JSON.stringify({ ...asset, _projectDir: projectIdFromDir(currentProjectDir()) });
                    event.dataTransfer.effectAllowed = "copy";
                    event.dataTransfer.setData("application/x-sonder-asset", payload);
                    event.dataTransfer.setData("text/plain", assetDisplayName(asset));
                });
                row.addEventListener("dragend", () => {
                    root.style.outline = "none";
                    clearDropFolderHighlight();
                });

                if (state.manageMode) {
                    const checkboxWrap = style(document.createElement("div"), `display:flex;align-items:center;justify-content:center;`);
                    checkboxWrap.addEventListener("mousedown", (event) => {
                        event.stopPropagation();
                    });
                    const checkbox = style(document.createElement("input"), `margin:0;cursor:pointer;`);
                    checkbox.type = "checkbox";
                    checkbox.checked = isSelected;
                    checkbox.addEventListener("click", (event) => {
                        event.stopPropagation();
                    });
                    checkbox.addEventListener("change", (event) => {
                        event.stopPropagation();
                        toggleAssetSelection(asset.asset_id, { focusList: true });
                    });
                    checkboxWrap.appendChild(checkbox);
                    row.appendChild(checkboxWrap);
                }

                const thumb = style(document.createElement("div"), `height:${thumbConfig.thumbHeight}px;border-radius:5px;background:${isMissing ? "#211714" : "#161616"};border:1px solid ${isMissing ? "#6f4a3d" : "#2e2e2e"};display:flex;align-items:center;justify-content:center;overflow:hidden;color:${isMissing ? "#ffb18c" : "#75818c"};font-size:${thumbConfig.metaFont}px;`);
                if (isMissing) {
                    thumb.textContent = "Missing";
                } else if (asset.has_thumbnail) {
                    const img = style(document.createElement("img"), `width:100%;height:100%;object-fit:cover;display:block;`);
                    img.src = buildThumbnailUrl(currentProjectDir(), asset.asset_id);
                    img.alt = assetDisplayName(asset);
                    img.draggable = false;
                    thumb.appendChild(img);
                } else {
                    thumb.textContent = assetFallbackGlyph(asset.asset_type);
                }

                const text = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:3px;justify-content:center;`);
                const name = style(document.createElement("div"), `color:#ececec;font-size:${thumbConfig.nameFont}px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                name.textContent = assetDisplayName(asset);
                if (isMissing) name.style.color = "#ffd0bc";
                const meta = style(document.createElement("div"), `color:#8ea0af;font-size:${thumbConfig.metaFont}px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                let metaLabel = asset.asset_type === "artifact"
                    ? `${isMissing ? "missing | " : ""}${assetKindLabel(asset.asset_type).toLowerCase()} | ${String(asset.artifact_kind || "other")} | ${assetExtension(asset) ? `.${assetExtension(asset)}` : "no ext"}`
                    : `${isMissing ? "missing | " : ""}${assetKindLabel(asset.asset_type).toLowerCase()} | ${formatResolution(asset)} | ${formatDuration(asset)}`;
                const workflowMeta = workflowStatusMeta(asset);
                if (workflowMeta) metaLabel += ` | ${workflowMeta}`;
                meta.textContent = metaLabel;
                text.append(name, meta);
                row.append(thumb, text);
                listScroller.appendChild(row);
            }
        }

        const trashedList = trashedAssets();
        if (trashedList.length) {
            renderedAnything = true;
            listScroller.appendChild(renderTrashFolderHeader(trashedList.length));

            if (!isFolderCollapsed(TRASH_FOLDER_COLLAPSE_KEY)) {
                for (const asset of trashedList) {
                    const isSelected = state.selectedAssetIds.has(asset.asset_id);
                    const isPrimary = state.selectedAssetId === asset.asset_id;
                    const isFocused = state.focusedAssetId === asset.asset_id;
                    const borderColor = isSelected
                        ? (isPrimary ? "#d39b86" : "#9f6d5f")
                        : (isFocused ? "#8e6f67" : "#5a433c");
                    const background = isSelected
                        ? "rgba(123,73,58,0.26)"
                        : (isFocused ? "rgba(123,73,58,0.17)" : "rgba(90,52,41,0.14)");
                    const focusRing = isPrimary
                        ? "box-shadow:inset 0 0 0 1px rgba(240,179,159,0.42);"
                        : (isFocused ? "box-shadow:inset 0 0 0 1px rgba(240,179,159,0.2);" : "");
                    const row = style(document.createElement("div"), `display:grid;grid-template-columns:${state.manageMode ? `24px ${thumbConfig.thumbWidth}px minmax(0,1fr)` : `${thumbConfig.thumbWidth}px minmax(0,1fr)`};gap:${thumbConfig.gap}px;padding:${thumbConfig.padding}px;border-radius:6px;border:1px solid ${borderColor};background:${background};cursor:pointer;opacity:0.92;${focusRing}`);
                    row.dataset.assetRow = asset.asset_id;
                    row.title = "Trashed asset. Restore to bring it back to its previous folder.";
                    row.addEventListener("click", (event) => {
                        handleAssetActivation(asset.asset_id, event, visibleAssets, { focusList: true, scrollIntoView: true });
                    });
                    row.addEventListener("contextmenu", (event) => {
                        event.preventDefault();
                        event.stopPropagation();
                        if (!(state.selectedAssetIds.has(asset.asset_id) && selectedAssetIdsList().length > 1)) {
                            selectAsset(asset.asset_id, { focusList: true });
                        }
                        showContextMenu(event.clientX, event.clientY, assetContextMenuItems(asset));
                    });
                    if (state.manageMode) {
                        const checkboxWrap = style(document.createElement("div"), `display:flex;align-items:center;justify-content:center;`);
                        const checkbox = style(document.createElement("input"), `margin:0;cursor:pointer;`);
                        checkbox.type = "checkbox";
                        checkbox.checked = isSelected;
                        checkbox.addEventListener("click", (event) => event.stopPropagation());
                        checkbox.addEventListener("change", (event) => {
                            event.stopPropagation();
                            toggleAssetSelection(asset.asset_id, { focusList: true });
                        });
                        checkboxWrap.appendChild(checkbox);
                        row.appendChild(checkboxWrap);
                    }

                    const thumb = style(document.createElement("div"), `height:${thumbConfig.thumbHeight}px;border-radius:5px;background:#1a1412;border:1px solid #6b4d43;display:flex;align-items:center;justify-content:center;overflow:hidden;color:#f0b39f;font-size:${thumbConfig.metaFont}px;`);
                    if (asset.has_thumbnail) {
                        const img = style(document.createElement("img"), `width:100%;height:100%;object-fit:cover;display:block;opacity:0.74;filter:saturate(0.6);`);
                        img.src = buildThumbnailUrl(currentProjectDir(), asset.asset_id);
                        img.alt = assetDisplayName(asset);
                        img.draggable = false;
                        thumb.appendChild(img);
                    } else {
                        thumb.textContent = assetFallbackGlyph(asset.asset_type);
                    }

                    const text = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:3px;justify-content:center;`);
                    const name = style(document.createElement("div"), `color:#f0d6cc;font-size:${thumbConfig.nameFont}px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                    name.textContent = assetDisplayName(asset);
                    const meta = style(document.createElement("div"), `color:#c0a49a;font-size:${thumbConfig.metaFont}px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                    const workflowMeta = workflowStatusMeta(asset);
                    meta.textContent = `trashed | ${assetKindLabel(asset.asset_type).toLowerCase()} | ${formatDate(asset.trashed_at)}${workflowMeta ? ` | ${workflowMeta}` : ""}`;
                    text.append(name, meta);
                    row.append(thumb, text);
                    listScroller.appendChild(row);
                }
            }
        }

        if (!renderedAnything) {
            const empty = style(document.createElement("div"), `padding:10px;border-radius:6px;background:rgba(255,255,255,0.04);color:#9aa6b2;font-size:10px;`);
            empty.textContent = data.assets.length ? "No assets match the current filter." : "No assets in this project yet. Drag files here or use Import.";
            listScroller.appendChild(empty);
        }

        if (state.inspectorCollapsed) {
            destroyLiveMedia();
            detailPane.innerHTML = "";
        } else {
            renderDetail(selected);
        }
        queueResize();
    }

    async function handleDrop(event) {
        if (!event.dataTransfer?.types?.includes("Files")) return;
        event.preventDefault();
        event.stopPropagation();
        root.style.outline = "none";
        hideContextMenu();
        const folder = normalizeFolderName(event.target.closest("[data-folder-drop]")?.dataset.folderDrop || "");
        try {
            const dropped = await collectDroppedFiles(event.dataTransfer);
            if (!dropped.length) return;
            const grouped = new Map();
            for (const item of dropped) {
                const targetFolder = folder || item.folder || "";
                if (!grouped.has(targetFolder)) grouped.set(targetFolder, []);
                grouped.get(targetFolder).push(item.file);
            }
            for (const [targetFolder, files] of grouped.entries()) {
                await options.onImportFiles?.(files, targetFolder);
            }
            await options.onRefresh?.();
        } catch (error) {
            console.warn("[Sonder] Failed to import dropped files:", error);
        }
    }

    fileInput.addEventListener("change", handleFileInputChange);
    replaceInput.addEventListener("change", handleReplaceInputChange);
    sortSelect.addEventListener("change", () => {
        state.sortMode = sortSelect.value || DEFAULT_SORT_MODE;
        persistSortMode();
        render();
    });
    inspectorBtn.addEventListener("click", () => {
        state.inspectorCollapsed = !state.inspectorCollapsed;
        persistInspectorCollapsed();
        render();
    });
    folderBtn.addEventListener("click", async () => {
        hideContextMenu();
        await promptCreateFolder();
    });
    manageBtn.addEventListener("click", () => {
        state.manageMode = !state.manageMode;
        persistManageMode();
        if (!state.manageMode) {
            reduceSelectionToPrimary({ focusList: true });
            return;
        }
        render();
    });
    searchInput.addEventListener("input", () => {
        state.query = searchInput.value || "";
        state.allowAutoFocus = true;
        render();
    });
    listScroller.addEventListener("mousedown", () => {
        hideContextMenu();
    });
    // Track last interaction inside the gallery surface so hasSelectionOwnership()
    // can recognize gallery-owned selection even when the click cleared focus to
    // <body> (asset rows are not tabindex-focusable).
    const stampGalleryInteraction = () => { state.lastInteractedAt = Date.now(); };
    root.addEventListener("mousedown", stampGalleryInteraction, true);
    const documentMouseDownClear = (event) => {
        if (root.contains(event.target)) return;
        if (state.overlayState.open && state.overlayState.overlayEl?.contains(event.target)) return;
        state.lastInteractedAt = 0;
    };
    document.addEventListener("mousedown", documentMouseDownClear, true);

    function hasSelectionOwnership() {
        if (state.destroyed) return false;
        if (!selectedAssetIdsList().length) return false;
        if (state.overlayState.open) return true;
        const active = document.activeElement;
        if (active && root.contains(active)) return true;
        return state.lastInteractedAt > 0;
    }

    function handleGallerySelectionDelete() {
        const selectionCount = selectedAssetIdsList().length;
        if (!selectionCount) return false;
        if (selectionCount > 1) {
            const selection = selectedAssets();
            const activeIds = selection.filter((asset) => !isTrashed(asset)).map((asset) => asset.asset_id);
            const trashedIds = selection.filter((asset) => isTrashed(asset)).map((asset) => asset.asset_id);
            if (activeIds.length) void handleBulkDelete(activeIds);
            else if (trashedIds.length) void handleBulkPermanentDelete(trashedIds);
            return true;
        }
        const asset = selectedAsset();
        if (!asset) return false;
        if (isTrashed(asset)) void handleAssetPermanentDelete(asset);
        else void handleAssetDelete(asset);
        return true;
    }

    function handleGallerySelectAll() {
        const visibleAssetList = ensureFocusedAsset(navigableAssets());
        const allVisibleIds = visibleAssetList.map((asset) => asset.asset_id);
        if (!allVisibleIds.length) return false;
        const primaryId = allVisibleIds.includes(state.selectedAssetId)
            ? state.selectedAssetId
            : allVisibleIds[allVisibleIds.length - 1];
        applySelection(allVisibleIds, primaryId, { focusList: true, scrollIntoView: true });
        return true;
    }

    function handleGalleryEscape() {
        const selectionCount = selectedAssetIdsList().length;
        if (!selectionCount) return false;
        hideContextMenu();
        if (selectionCount > 1) {
            reduceSelectionToPrimary({ focusList: true, scrollIntoView: true });
        } else {
            clearSelection();
        }
        return true;
    }

    // GALLERY consumer (priority 50). Routes Delete, Ctrl+A, and Escape to the
    // existing list handlers when the gallery owns the current selection. Falls
    // through (returns false) otherwise so the EDITOR consumer or LiteGraph can
    // claim the event. Element-level listScroller listener still owns arrow
    // nav, Enter, and Space → inspect when the list itself has focus.
    const galleryKeyOff = registerKeyboardConsumer({
        id: consumerId("list"),
        priority: KEY_PRIORITY.GALLERY,
        keydown: (event) => {
            if (state.destroyed) return false;
            if (event.target?.closest?.("input, textarea, select")) return false;
            if (!hasSelectionOwnership()) return false;
            const key = event.key;
            if ((event.ctrlKey || event.metaKey) && String(key || "").toLowerCase() === "a") {
                return handleGallerySelectAll();
            }
            if (key === "Delete") return handleGallerySelectionDelete();
            if (key === "Escape") return handleGalleryEscape();
            return false;
        },
    });
    // Element-level keydown handles focus-driven list navigation when the
    // listScroller itself has DOM focus. Delete / Ctrl+A / Escape route through
    // the GALLERY consumer (priority 50) instead, so they fire even when the
    // user clicked an asset row that did not become activeElement.
    listScroller.addEventListener("keydown", (event) => {
        if (event.target !== listScroller) return;

        const visibleAssetList = ensureFocusedAsset(navigableAssets());

        if (LIST_NAV_KEYS.has(event.key)) {
            event.preventDefault();
            if (!visibleAssetList.length) return;
            const direction = event.key === "ArrowUp" || event.key === "ArrowLeft" ? -1 : 1;
            const currentIndex = visibleAssetList.findIndex((asset) => asset.asset_id === state.focusedAssetId);
            const nextIndex = Math.min(
                visibleAssetList.length - 1,
                Math.max(0, (currentIndex >= 0 ? currentIndex : 0) + direction),
            );
            const nextAssetId = visibleAssetList[nextIndex]?.asset_id || "";
            selectAsset(nextAssetId, { scrollIntoView: true });
            return;
        }

        if (event.key === "Enter") {
            event.preventDefault();
            if (!state.focusedAssetId) return;
            selectAsset(state.focusedAssetId, { scrollIntoView: true });
            return;
        }

        if (event.key === " " || event.key === "Spacebar") {
            event.preventDefault();
            if (!state.focusedAssetId) return;
            const focusedAsset = data.assets.find((asset) => asset.asset_id === state.focusedAssetId);
            if (focusedAsset) {
                openInspectOverlay(focusedAsset);
            }
            return;
        }
    });
    root.addEventListener("dragover", (event) => {
        if (!dataTransferHasType(event.dataTransfer, "Files")) return;
        event.preventDefault();
        event.stopPropagation();
        event.dataTransfer.dropEffect = "copy";
        root.style.outline = "2px dashed rgba(100, 180, 255, 0.6)";
        const folderTarget = event.target.closest("[data-folder-drop]");
        if (state.dropFolder !== folderTarget?.dataset.folderDrop) {
            setDropFolderHighlight(folderTarget?.dataset.folderDrop || "");
        }
    });
    root.addEventListener("dragleave", (event) => {
        if (event.currentTarget !== event.target) return;
        root.style.outline = "none";
        clearDropFolderHighlight();
    });
    root.addEventListener("drop", async (event) => {
        clearDropFolderHighlight();
        await handleDrop(event);
    });
    root.addEventListener("contextmenu", (event) => {
        if (event.target.closest("input, textarea, button, select, option, video, audio")) return;
        if (event.target.closest("[data-asset-row], [data-folder-header]")) return;
        event.preventDefault();
        event.stopPropagation();
        showContextMenu(event.clientX, event.clientY, backgroundContextMenuItems());
    });

    const resizeObserver = typeof ResizeObserver === "function"
        ? new ResizeObserver(() => {
            updateLayout();
            queueResize();
        })
        : null;
    resizeObserver?.observe(root);

    function setData(nextData) {
        const payload = Array.isArray(nextData) ? { assets: nextData, folders: [] } : (nextData || {});
        data.assets = Array.isArray(payload.assets) ? [...payload.assets] : [];
        data.folders = Array.isArray(payload.folders) ? payload.folders.map(normalizeFolderName).filter(Boolean) : [];
        const preservedSelection = selectedAssetIdsList();
        const fallbackId = data.assets.some((asset) => asset.asset_id === state.selectedAssetId)
            ? state.selectedAssetId
            : (preservedSelection[preservedSelection.length - 1] || data.assets[0]?.asset_id || "");
        applySelectionState(preservedSelection.length ? preservedSelection : (fallbackId ? [fallbackId] : []), fallbackId);
        if (!data.assets.some((asset) => asset.asset_id === state.focusedAssetId)) {
            state.focusedAssetId = data.assets[0]?.asset_id || "";
        }
        if (state.showingUsagesFor && !data.assets.some((asset) => asset.asset_id === state.showingUsagesFor)) {
            clearUsageView();
        }
        state.allowAutoFocus = true;
        render();
    }

    function destroy() {
        if (state.destroyed) return;
        state.destroyed = true;
        galleryKeyOff();
        document.removeEventListener("mousedown", documentMouseDownClear, true);
        unsubscribeSettings();
        resizeObserver?.disconnect();
        closeInspectOverlay();
        hideContextMenu();
        destroyLiveMedia();
    }

    setData(options.initialData || { assets: [], folders: [] });
    return {
        root,
        setData,
        destroy,
        isInspectOverlayOpen: () => !!state.overlayState.open,
        hasSelectionOwnership,
        revealAsset,
    };
}
