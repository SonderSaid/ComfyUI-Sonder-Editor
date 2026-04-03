const { api } = window.comfyAPI.api;

const DEFAULT_SORT_MODE = "newest";
const ROOT_FOLDER_COLLAPSE_KEY = "__ltx_root__";
const SORT_OPTIONS = [
    { value: "newest", label: "Newest" },
    { value: "oldest", label: "Oldest" },
    { value: "name", label: "Name" },
    { value: "type", label: "Type" },
    { value: "duration", label: "Duration" },
    { value: "resolution", label: "Resolution" },
];
const LIST_NAV_KEYS = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);

function style(el, cssText) {
    el.style.cssText = cssText;
    return el;
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
    const subfolder = `ltx_projects/${dirName}/${subPath}`;
    return api.apiURL(`/view?filename=${encodeURIComponent(fileName)}&subfolder=${encodeURIComponent(subfolder)}&type=output`);
}

function buildThumbnailUrl(projectDir, assetId) {
    return api.apiURL(`/ltx-editor/project/${projectIdFromDir(projectDir)}/thumbnail/${assetId}`);
}

function assetKindLabel(type) {
    if (type === "video") return "Video";
    if (type === "image") return "Image";
    if (type === "audio") return "Audio";
    return "Asset";
}

function assetFallbackGlyph(type) {
    if (type === "video") return "V";
    if (type === "image") return "I";
    if (type === "audio") return "A";
    return "A";
}

function assetDisplayName(asset) {
    return asset?.name || asset?.path?.split(/[/\\]/).pop() || "Untitled";
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

function formatDate(value) {
    if (!value) return "-";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
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

function makeMetaCell(label, value) {
    const cell = style(document.createElement("div"), `padding:6px;border-radius:6px;background:rgba(255,255,255,0.03);min-width:0;`);
    const title = style(document.createElement("div"), `color:#7f8b96;margin-bottom:2px;font-size:10px;`);
    title.textContent = label;
    const content = style(document.createElement("div"), `color:#ececec;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
    content.textContent = value;
    cell.append(title, content);
    return cell;
}

function makeSectionTitle(label) {
    const title = style(document.createElement("div"), `color:#a9b8c4;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;`);
    title.textContent = label;
    return title;
}

export function mountSharedAssetGallery(container, options = {}) {
    const state = {
        type: "all",
        query: "",
        selectedAssetId: "",
        focusedAssetId: "",
        allowAutoFocus: true,
        liveMedia: null,
        dropFolder: "",
        destroyed: false,
        collapsedFolders: new Set(),
        inspectorCollapsed: false,
        sortMode: DEFAULT_SORT_MODE,
        storageProjectId: "",
        contextMenuEl: null,
        contextMenuCleanup: null,
    };
    const data = { assets: [], folders: [] };
    const root = style(document.createElement("div"), `display:flex;flex-direction:column;gap:8px;min-width:0;width:100%;height:100%;box-sizing:border-box;`);
    container.appendChild(root);

    const fileInput = document.createElement("input");
    fileInput.type = "file";
    fileInput.multiple = true;
    fileInput.style.display = "none";
    root.appendChild(fileInput);

    const controls = style(document.createElement("div"), `display:flex;gap:6px;align-items:center;min-width:0;flex-wrap:wrap;`);
    const searchInput = style(document.createElement("input"), `flex:1 1 180px;min-width:120px;background:#171717;border:1px solid #3d3d3d;border-radius:6px;color:#ececec;padding:6px 8px;font-size:11px;`);
    searchInput.type = "search";
    searchInput.placeholder = "Search assets";
    controls.appendChild(searchInput);

    const sortSelect = style(document.createElement("select"), `flex:0 0 auto;min-width:110px;background:#171717;border:1px solid #3d3d3d;border-radius:6px;color:#ececec;padding:6px 8px;font-size:11px;`);
    for (const option of SORT_OPTIONS) {
        const optionEl = document.createElement("option");
        optionEl.value = option.value;
        optionEl.textContent = option.label;
        sortSelect.appendChild(optionEl);
    }
    controls.appendChild(sortSelect);

    const makeActionButton = () => style(document.createElement("button"), `padding:6px 10px;border-radius:6px;border:1px solid #47627a;background:#25384a;color:#fff;cursor:pointer;font-size:11px;font-weight:600;`);

    const inspectorBtn = makeActionButton();
    inspectorBtn.style.background = "#1d2630";
    inspectorBtn.style.borderColor = "#364655";
    controls.appendChild(inspectorBtn);

    const importBtn = makeActionButton();
    importBtn.textContent = "Import";
    importBtn.addEventListener("click", () => fileInput.click());
    controls.appendChild(importBtn);

    const folderBtn = makeActionButton();
    folderBtn.textContent = "Folder";
    controls.appendChild(folderBtn);

    const refreshBtn = makeActionButton();
    refreshBtn.textContent = "Refresh";
    refreshBtn.style.background = "#1d2630";
    refreshBtn.style.borderColor = "#364655";
    refreshBtn.addEventListener("click", async () => {
        hideContextMenu();
        await options.onRefresh?.();
    });
    controls.appendChild(refreshBtn);

    root.appendChild(controls);

    const tabsRow = style(document.createElement("div"), `display:flex;flex-wrap:wrap;gap:6px;`);
    root.appendChild(tabsRow);

    const content = style(document.createElement("div"), `display:grid;grid-template-columns:minmax(0,1.2fr) minmax(260px,1fr);gap:8px;align-items:stretch;min-height:0;flex:1;overflow:hidden;`);
    root.appendChild(content);

    const listPane = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:6px;min-height:0;`);
    const listScroller = style(document.createElement("div"), `overflow-y:auto;display:flex;flex-direction:column;gap:6px;padding-right:2px;min-height:0;outline:none;${options.maxListHeight ? `max-height:${options.maxListHeight}px;` : "flex:1;"}`);
    listScroller.tabIndex = 0;
    listPane.appendChild(listScroller);

    const detailPane = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:8px;padding:8px;border-radius:6px;background:rgba(255,255,255,0.03);border:1px solid #343434;min-height:0;overflow:auto;`);
    content.append(listPane, detailPane);

    const folderListId = `ltx-gallery-folders-${Math.random().toString(36).slice(2)}`;
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
        return `ltx-gallery-${currentProjectId()}-${suffix}`;
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
        state.inspectorCollapsed = !!readStoredJson(storageKey("inspector-collapsed"), false);
        const storedSort = readStoredJson(storageKey("sort-mode"), DEFAULT_SORT_MODE);
        state.sortMode = SORT_OPTIONS.some((entry) => entry.value === storedSort) ? storedSort : DEFAULT_SORT_MODE;
    }

    function persistCollapsedFolders() {
        safeStorageSet(storageKey("collapsed-folders"), JSON.stringify(Array.from(state.collapsedFolders)));
    }

    function persistInspectorCollapsed() {
        safeStorageSet(storageKey("inspector-collapsed"), JSON.stringify(!!state.inspectorCollapsed));
    }

    function persistSortMode() {
        safeStorageSet(storageKey("sort-mode"), JSON.stringify(state.sortMode));
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
    }

    function setBusyButton(btn, isBusy, busyLabel, idleLabel) {
        btn.disabled = isBusy;
        btn.textContent = isBusy ? busyLabel : idleLabel;
        btn.style.opacity = isBusy ? "0.75" : "1";
    }

    function queueResize() {
        options.onRequestResize?.();
    }

    function destroyLiveMedia() {
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
            position: fixed; left: ${x}px; top: ${y}px; z-index: 10000;
            background: #2a2a2a; border: 1px solid #555; border-radius: 4px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.5); min-width: 160px;
            padding: 4px 0; font-size: 11px;
        `);

        for (const item of items) {
            if (!item) continue;
            if (item.type === "separator") {
                menu.appendChild(style(document.createElement("div"), `height:1px;background:#404040;margin:4px 0;`));
                continue;
            }
            const row = document.createElement("div");
            row.textContent = item.label;
            const isDisabled = !!item.disabled;
            row.style.cssText = `
                padding: 6px 14px; cursor: ${isDisabled ? "default" : "pointer"};
                color: ${isDisabled ? "#666" : (item.danger ? "#f66" : "#ddd")};
            `;
            if (!isDisabled) {
                row.addEventListener("mouseenter", () => {
                    row.style.background = "#3a3a3a";
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
        const escHandler = (event) => {
            if (event.key === "Escape") hideContextMenu();
        };
        const scrollHandler = () => hideContextMenu();
        state.contextMenuCleanup = () => {
            document.removeEventListener("mousedown", closeHandler);
            document.removeEventListener("keydown", escHandler);
            window.removeEventListener("scroll", scrollHandler, true);
        };
        setTimeout(() => {
            document.addEventListener("mousedown", closeHandler);
            document.addEventListener("keydown", escHandler);
            window.addEventListener("scroll", scrollHandler, true);
        }, 10);
    }

    function allFolders() {
        const folders = new Set((data.folders || []).map(normalizeFolderName).filter(Boolean));
        for (const asset of data.assets) {
            const folder = normalizeFolderName(asset.folder || "");
            if (folder) folders.add(folder);
        }
        if (!folders.size && !data.assets.length) return [];
        return ["", ...Array.from(folders).sort(compareStrings)];
    }

    function isFolderCollapsed(folderName) {
        return state.collapsedFolders.has(folderCollapseKey(folderName));
    }

    function toggleFolderCollapsed(folderName) {
        const key = folderCollapseKey(folderName);
        const willCollapse = !state.collapsedFolders.has(key);
        if (!willCollapse) {
            state.collapsedFolders.delete(key);
        } else {
            state.collapsedFolders.add(key);
            const selected = data.assets.find((asset) => asset.asset_id === state.selectedAssetId);
            if (selected && folderCollapseKey(selected.folder) === key) {
                const nextVisibleAsset = visibleNavigableAssets(filteredAssets())[0] || null;
                state.selectedAssetId = nextVisibleAsset?.asset_id || "";
                state.focusedAssetId = nextVisibleAsset?.asset_id || "";
            }
        }
        persistCollapsedFolders();
        render();
    }

    function filteredAssets() {
        const query = state.query.trim().toLowerCase();
        return data.assets.filter((asset) => {
            if (state.type !== "all" && asset.asset_type !== state.type) return false;
            if (!query) return true;
            const haystack = [
                asset.name,
                asset.path,
                asset.folder,
                asset.asset_type,
                asset.prompt,
                formatGenerationValue(asset.generation_params),
            ].filter(Boolean).join(" ").toLowerCase();
            return haystack.includes(query);
        }).sort((left, right) => {
            if (state.sortMode === "oldest") {
                return assetImportedAt(left) - assetImportedAt(right) || compareStrings(assetDisplayName(left), assetDisplayName(right));
            }
            if (state.sortMode === "name") {
                return compareStrings(assetDisplayName(left), assetDisplayName(right)) || (assetImportedAt(right) - assetImportedAt(left));
            }
            if (state.sortMode === "type") {
                return compareStrings(left.asset_type, right.asset_type) || compareStrings(assetDisplayName(left), assetDisplayName(right));
            }
            if (state.sortMode === "duration") {
                return assetDurationSortValue(right) - assetDurationSortValue(left) || compareStrings(assetDisplayName(left), assetDisplayName(right));
            }
            if (state.sortMode === "resolution") {
                return assetResolutionSortValue(right) - assetResolutionSortValue(left) || compareStrings(assetDisplayName(left), assetDisplayName(right));
            }
            return assetImportedAt(right) - assetImportedAt(left) || compareStrings(assetDisplayName(left), assetDisplayName(right));
        });
    }

    function folderAssets(folderName, assets) {
        return assets.filter((asset) => normalizeFolderName(asset.folder || "") === normalizeFolderName(folderName));
    }

    function visibleNavigableAssets(assets) {
        return assets.filter((asset) => !isFolderCollapsed(asset.folder));
    }

    function selectedAsset(assets) {
        return assets.find((item) => item.asset_id === state.selectedAssetId) || null;
    }

    function ensureFocusedAsset(assets) {
        const navigable = visibleNavigableAssets(assets);
        if (!navigable.length) {
            state.focusedAssetId = "";
            return navigable;
        }
        if (navigable.some((asset) => asset.asset_id === state.focusedAssetId)) return navigable;
        if (state.allowAutoFocus) state.focusedAssetId = navigable[0].asset_id;
        return navigable;
    }

    function updateLayout() {
        const width = root.getBoundingClientRect().width || 0;
        const singleColumn = state.inspectorCollapsed || (width > 0 && width < 520);
        content.style.gridTemplateColumns = singleColumn
            ? "minmax(0,1fr)"
            : "minmax(0,1.2fr) minmax(260px,1fr)";
        detailPane.style.display = state.inspectorCollapsed ? "none" : "flex";
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
        render();
    }

    function renderGenerationSection(asset) {
        const wrap = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;`);
        if (asset.prompt) {
            wrap.appendChild(makeSectionTitle("Prompt"));
            const promptBox = style(document.createElement("div"), `padding:8px;border-radius:6px;background:rgba(255,255,255,0.03);border:1px solid #343434;color:#d9e0e6;font-size:10px;line-height:1.45;white-space:pre-wrap;`);
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
            const empty = style(document.createElement("div"), `color:#7f8b96;font-size:10px;font-style:italic;`);
            empty.textContent = "No generation metadata stored for this asset.";
            wrap.appendChild(empty);
        }
        return wrap;
    }

    function renderDetail(asset) {
        destroyLiveMedia();
        detailPane.innerHTML = "";
        if (!asset) {
            const emptyTitle = style(document.createElement("div"), `color:#ececec;font-size:11px;font-weight:700;`);
            emptyTitle.textContent = "Asset Details";
            const emptyText = style(document.createElement("div"), `color:#8ea0af;font-size:10px;line-height:1.45;`);
            emptyText.textContent = "Select an asset to inspect it. Right-click the gallery background to create folders or import files.";
            detailPane.append(emptyTitle, emptyText);
            queueResize();
            return;
        }

        const titleRow = style(document.createElement("div"), `display:flex;align-items:center;justify-content:space-between;gap:8px;min-width:0;`);
        const title = style(document.createElement("div"), `color:#ececec;font-size:11px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        title.textContent = assetDisplayName(asset);
        const kind = style(document.createElement("div"), `padding:3px 6px;border-radius:999px;background:rgba(143,192,240,0.12);border:1px solid rgba(143,192,240,0.28);color:#9fc8ea;font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:0.05em;white-space:nowrap;`);
        kind.textContent = assetKindLabel(asset.asset_type);
        titleRow.append(title, kind);
        const pathLine = style(document.createElement("div"), `color:#7f8b96;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
        pathLine.textContent = asset.path || "-";

        const previewSurface = style(document.createElement("div"), `min-height:140px;border-radius:6px;border:1px solid #2f2f2f;background:#111;overflow:hidden;display:flex;align-items:center;justify-content:center;position:relative;`);
        const projectDir = currentProjectDir();
        if (asset.asset_type === "image") {
            const img = style(document.createElement("img"), `width:100%;max-height:220px;object-fit:contain;display:block;`);
            img.src = buildAssetViewUrl(projectDir, asset.path);
            img.alt = assetDisplayName(asset);
            previewSurface.appendChild(img);
        } else if (asset.asset_type === "video") {
            const video = style(document.createElement("video"), `width:100%;max-height:220px;display:block;background:#000;`);
            video.controls = true;
            video.preload = "metadata";
            video.playsInline = true;
            video.src = buildAssetViewUrl(projectDir, asset.path);
            if (asset.has_thumbnail) video.poster = buildThumbnailUrl(projectDir, asset.asset_id);
            previewSurface.appendChild(video);
            state.liveMedia = video;
        } else if (asset.asset_type === "audio") {
            const audioWrap = style(document.createElement("div"), `width:100%;padding:16px;display:flex;flex-direction:column;gap:12px;align-items:stretch;box-sizing:border-box;`);
            const audioLabel = style(document.createElement("div"), `color:#9fb6c8;font-size:10px;text-transform:uppercase;letter-spacing:0.08em;`);
            audioLabel.textContent = "Audio Preview";
            const audio = style(document.createElement("audio"), `width:100%;display:block;`);
            audio.controls = true;
            audio.preload = "metadata";
            audio.src = buildAssetViewUrl(projectDir, asset.path);
            audioWrap.append(audioLabel, audio);
            previewSurface.appendChild(audioWrap);
            state.liveMedia = audio;
        } else {
            const placeholder = style(document.createElement("div"), `color:#8ea0af;font-size:10px;`);
            placeholder.textContent = "Preview unavailable.";
            previewSurface.appendChild(placeholder);
        }

        const meta = style(document.createElement("div"), `display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px;font-size:10px;color:#b9c3cb;`);
        const rows = [
            ["Type", assetKindLabel(asset.asset_type)],
            ["Folder", normalizeFolderName(asset.folder) || "Root"],
            ["Size", formatResolution(asset)],
            ["Duration", formatDuration(asset)],
            ["FPS", asset.fps ? String(asset.fps) : "-"],
            ["Sample Rate", asset.sample_rate ? `${asset.sample_rate} Hz` : "-"],
            ["Embedded Audio", asset.has_audio ? "Yes" : "No"],
            ["Imported", formatDate(asset.imported_at)],
        ];
        for (const [label, value] of rows) meta.appendChild(makeMetaCell(label, value));

        const form = style(document.createElement("div"), `display:flex;flex-direction:column;gap:6px;`);
        const nameLabel = makeSectionTitle("Display Name");
        const nameInput = style(document.createElement("input"), `background:#171717;border:1px solid #3d3d3d;border-radius:6px;color:#ececec;padding:6px 8px;font-size:11px;`);
        nameInput.value = asset.name || "";
        const renameHint = style(document.createElement("div"), `color:#7f8b96;font-size:10px;line-height:1.35;`);
        renameHint.textContent = "This only changes the display name. The file path and extension on disk stay unchanged.";
        const folderLabel = makeSectionTitle("Folder");
        const folderInput = style(document.createElement("input"), `background:#171717;border:1px solid #3d3d3d;border-radius:6px;color:#ececec;padding:6px 8px;font-size:11px;`);
        folderInput.value = normalizeFolderName(asset.folder);
        folderInput.placeholder = "Root";
        folderInput.setAttribute("list", folderListId);
        const saveBtn = makeActionButton();
        saveBtn.textContent = "Save Metadata";
        saveBtn.style.alignSelf = "flex-start";
        saveBtn.addEventListener("click", async () => {
            try {
                setBusyButton(saveBtn, true, "Saving...", "Save Metadata");
                await applyAssetUpdate(asset, { name: nameInput.value, folder: folderInput.value });
            } catch (error) {
                console.warn("[LTX Editor] Failed to save asset metadata:", error);
            } finally {
                setBusyButton(saveBtn, false, "Saving...", "Save Metadata");
            }
        });
        form.append(nameLabel, nameInput, renameHint, folderLabel, folderInput, saveBtn);

        detailPane.append(titleRow, pathLine, previewSurface, meta, renderGenerationSection(asset), form);
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
            console.warn("[LTX Editor] Failed to create folder:", error);
        }
    }

    async function handleAssetRename(asset) {
        const nextName = prompt("Display name:", asset.name || assetDisplayName(asset));
        if (nextName === null) return;
        await applyAssetUpdate(asset, { name: nextName.trim() });
    }

    async function handleAssetMoveToFolder(asset) {
        const nextFolder = prompt("Move asset to folder (blank for Root):", normalizeFolderName(asset.folder));
        if (nextFolder === null) return;
        await applyAssetUpdate(asset, { folder: nextFolder });
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
            { label: "Rename Folder", disabled: true },
            { label: "Delete Folder", disabled: true },
        ];
    }

    function assetContextMenuItems(asset) {
        return [
            {
                label: "Rename",
                action: async () => {
                    state.selectedAssetId = asset.asset_id;
                    state.focusedAssetId = asset.asset_id;
                    state.allowAutoFocus = true;
                    render();
                    await handleAssetRename(asset);
                },
            },
            {
                label: "Move to Folder...",
                action: async () => {
                    state.selectedAssetId = asset.asset_id;
                    state.focusedAssetId = asset.asset_id;
                    state.allowAutoFocus = true;
                    render();
                    await handleAssetMoveToFolder(asset);
                },
            },
            { type: "separator" },
            { label: "Delete", disabled: true },
        ];
    }

    function renderFolderHeader(folderName, assetCount) {
        const normalized = normalizeFolderName(folderName);
        const collapsed = isFolderCollapsed(normalized);
        const header = style(document.createElement("div"), `display:flex;align-items:center;justify-content:space-between;gap:8px;padding:5px 8px;border-radius:6px;background:${normalized ? "#1a1a2e" : "#1f2530"};color:#8fc0f0;font-size:10px;font-weight:600;border:1px solid transparent;`);
        header.dataset.folderDrop = normalized;
        header.dataset.folderHeader = normalized || "__root__";
        header.title = normalized ? "Drop files here to import into this folder." : "Root folder";

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

    function render() {
        if (state.destroyed) return;
        ensureProjectPrefs();
        updateControlState();
        updateLayout();
        updateFolderOptions();
        tabsRow.innerHTML = "";
        listScroller.innerHTML = "";

        const counts = {
            all: data.assets.length,
            video: data.assets.filter((asset) => asset.asset_type === "video").length,
            image: data.assets.filter((asset) => asset.asset_type === "image").length,
            audio: data.assets.filter((asset) => asset.asset_type === "audio").length,
        };
        const tabs = [
            ["all", `All (${counts.all})`],
            ["video", `Videos (${counts.video})`],
            ["image", `Images (${counts.image})`],
            ["audio", `Audio (${counts.audio})`],
        ];
        for (const [type, label] of tabs) {
            const isActive = state.type === type;
            const tab = style(document.createElement("button"), `padding:5px 8px;border-radius:6px;border:1px solid ${isActive ? "#6f8ea8" : "#444"};background:${isActive ? "#4a5c6b" : "#232323"};color:${isActive ? "#fff" : "#d2d2d2"};cursor:pointer;font-size:10px;`);
            tab.textContent = label;
            tab.addEventListener("click", () => {
                state.type = type;
                state.allowAutoFocus = true;
                render();
            });
            tabsRow.appendChild(tab);
        }

        const assets = filteredAssets();
        ensureFocusedAsset(assets);
        const selected = selectedAsset(assets);
        const folders = allFolders();
        let renderedAnything = false;

        for (const folderName of folders) {
            const inFolder = folderAssets(folderName, assets);
            renderedAnything = true;
            listScroller.appendChild(renderFolderHeader(folderName, inFolder.length));

            if (isFolderCollapsed(folderName)) continue;

            if (!inFolder.length) {
                const emptyFolder = style(document.createElement("div"), `padding:8px 10px;border-radius:6px;border:1px dashed #3b4650;color:#7f8b96;font-size:10px;margin-bottom:2px;`);
                emptyFolder.textContent = folderName ? "Empty folder. Drop files here to import into it." : "No root assets match the current filter.";
                listScroller.appendChild(emptyFolder);
                continue;
            }

            for (const asset of inFolder) {
                const isSelected = state.selectedAssetId === asset.asset_id;
                const isFocused = state.focusedAssetId === asset.asset_id;
                const borderColor = isSelected
                    ? "#6f8ea8"
                    : (isFocused ? "#7f8b96" : "#373737");
                const background = isSelected
                    ? "rgba(75,105,135,0.18)"
                    : (isFocused ? "rgba(255,255,255,0.05)" : "rgba(255,255,255,0.03)");
                const focusRing = isFocused
                    ? "box-shadow:inset 0 0 0 1px rgba(143,192,240,0.25);"
                    : "";
                const row = style(document.createElement("div"), `display:grid;grid-template-columns:72px minmax(0,1fr);gap:8px;padding:6px;border-radius:6px;border:1px solid ${borderColor};background:${background};cursor:pointer;${focusRing}`);
                row.dataset.assetRow = asset.asset_id;
                row.draggable = true;
                row.title = "Click to inspect. Drag onto the graph to create a loader node.";
                row.addEventListener("click", () => {
                    state.selectedAssetId = asset.asset_id;
                    state.focusedAssetId = asset.asset_id;
                    state.allowAutoFocus = true;
                    render();
                    requestAnimationFrame(() => {
                        listScroller.focus({ preventScroll: true });
                    });
                    scrollAssetIntoView(asset.asset_id);
                });
                row.addEventListener("contextmenu", (event) => {
                    event.preventDefault();
                    event.stopPropagation();
                    state.selectedAssetId = asset.asset_id;
                    state.focusedAssetId = asset.asset_id;
                    state.allowAutoFocus = true;
                    render();
                    requestAnimationFrame(() => {
                        listScroller.focus({ preventScroll: true });
                    });
                    showContextMenu(event.clientX, event.clientY, assetContextMenuItems(asset));
                });
                row.addEventListener("dragstart", (event) => {
                    const payload = JSON.stringify({ ...asset, _projectDir: projectIdFromDir(currentProjectDir()) });
                    event.dataTransfer.effectAllowed = "copy";
                    event.dataTransfer.setData("application/ltx-asset", payload);
                    event.dataTransfer.setData("text/plain", assetDisplayName(asset));
                });

                const thumb = style(document.createElement("div"), `height:54px;border-radius:5px;background:#161616;border:1px solid #2e2e2e;display:flex;align-items:center;justify-content:center;overflow:hidden;color:#75818c;font-size:10px;`);
                if (asset.has_thumbnail) {
                    const img = style(document.createElement("img"), `width:100%;height:100%;object-fit:cover;display:block;`);
                    img.src = buildThumbnailUrl(currentProjectDir(), asset.asset_id);
                    img.alt = assetDisplayName(asset);
                    img.draggable = false;
                    thumb.appendChild(img);
                } else {
                    thumb.textContent = assetFallbackGlyph(asset.asset_type);
                }

                const text = style(document.createElement("div"), `min-width:0;display:flex;flex-direction:column;gap:3px;justify-content:center;`);
                const name = style(document.createElement("div"), `color:#ececec;font-size:11px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                name.textContent = assetDisplayName(asset);
                const meta = style(document.createElement("div"), `color:#8ea0af;font-size:10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;`);
                meta.textContent = `${assetKindLabel(asset.asset_type).toLowerCase()} | ${formatResolution(asset)} | ${formatDuration(asset)}`;
                text.append(name, meta);
                row.append(thumb, text);
                listScroller.appendChild(row);
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
            console.warn("[LTX Editor] Failed to import dropped files:", error);
        }
    }

    fileInput.addEventListener("change", handleFileInputChange);
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
    searchInput.addEventListener("input", () => {
        state.query = searchInput.value || "";
        state.allowAutoFocus = true;
        render();
    });
    listScroller.addEventListener("mousedown", () => {
        hideContextMenu();
    });
    listScroller.addEventListener("keydown", (event) => {
        if (event.target !== listScroller) return;

        const assets = filteredAssets();
        const navigableAssets = ensureFocusedAsset(assets);

        if (LIST_NAV_KEYS.has(event.key)) {
            event.preventDefault();
            if (!navigableAssets.length) return;
            state.allowAutoFocus = true;
            const direction = event.key === "ArrowUp" || event.key === "ArrowLeft" ? -1 : 1;
            const currentIndex = navigableAssets.findIndex((asset) => asset.asset_id === state.focusedAssetId);
            const nextIndex = Math.min(
                navigableAssets.length - 1,
                Math.max(0, (currentIndex >= 0 ? currentIndex : 0) + direction),
            );
            const nextAssetId = navigableAssets[nextIndex]?.asset_id || "";
            state.focusedAssetId = nextAssetId;
            state.selectedAssetId = nextAssetId;
            render();
            scrollAssetIntoView(state.focusedAssetId);
            return;
        }

        if (event.key === "Enter") {
            event.preventDefault();
            if (!state.focusedAssetId) return;
            state.selectedAssetId = state.focusedAssetId;
            state.allowAutoFocus = true;
            render();
            scrollAssetIntoView(state.focusedAssetId);
            return;
        }

        if (event.key === "Escape") {
            event.preventDefault();
            state.selectedAssetId = "";
            state.focusedAssetId = "";
            state.allowAutoFocus = false;
            hideContextMenu();
            render();
            return;
        }

        if (event.key === "Delete") {
            event.preventDefault();
        }
    });
    root.addEventListener("dragover", (event) => {
        if (!event.dataTransfer?.types?.includes("Files")) return;
        event.preventDefault();
        event.stopPropagation();
        event.dataTransfer.dropEffect = "copy";
        root.style.outline = "2px dashed rgba(100, 180, 255, 0.6)";
        const folderTarget = event.target.closest("[data-folder-drop]");
        if (state.dropFolder !== folderTarget?.dataset.folderDrop) {
            state.dropFolder = folderTarget?.dataset.folderDrop || "";
            for (const el of root.querySelectorAll("[data-folder-drop]")) {
                el.style.borderColor = el.dataset.folderDrop === state.dropFolder ? "#6f8ea8" : "transparent";
            }
        }
    });
    root.addEventListener("dragleave", (event) => {
        if (event.currentTarget !== event.target) return;
        root.style.outline = "none";
        state.dropFolder = "";
        for (const el of root.querySelectorAll("[data-folder-drop]")) el.style.borderColor = "transparent";
    });
    root.addEventListener("drop", async (event) => {
        state.dropFolder = "";
        for (const el of root.querySelectorAll("[data-folder-drop]")) el.style.borderColor = "transparent";
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
        if (!data.assets.some((asset) => asset.asset_id === state.selectedAssetId)) {
            state.selectedAssetId = data.assets[0]?.asset_id || "";
        }
        if (!data.assets.some((asset) => asset.asset_id === state.focusedAssetId)) {
            state.focusedAssetId = data.assets[0]?.asset_id || "";
        }
        state.allowAutoFocus = true;
        render();
    }

    function destroy() {
        if (state.destroyed) return;
        state.destroyed = true;
        resizeObserver?.disconnect();
        hideContextMenu();
        destroyLiveMedia();
    }

    setData(options.initialData || { assets: [], folders: [] });
    return { root, setData, destroy };
}
