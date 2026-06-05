import {
    EDITOR_CHROME as CHROME,
    FONT,
    THEME,
    chromeButtonCss,
    statusPillCss,
} from "./editor_theme.js";

function style(el, cssText) {
    el.style.cssText = cssText;
    return el;
}

function asArray(value) {
    return Array.isArray(value) ? value : [];
}

function asCollapsedSet(value) {
    if (value instanceof Set) {
        return new Set(Array.from(value).filter((entry) => typeof entry === "string" && entry));
    }
    if (Array.isArray(value)) {
        return new Set(value.filter((entry) => typeof entry === "string" && entry));
    }
    return new Set();
}

export function statusStateForQueue(status) {
    const normalized = String(status || "pending").trim().toLowerCase();
    if (normalized === "running") return "running";
    if (normalized === "pending") return "pending";
    if (normalized === "failed") return "failed";
    if (normalized === "completed") return "completed";
    return "idle";
}

export function formatQueueStatusLabel(status) {
    const raw = String(status || "pending").trim().toLowerCase();
    if (!raw) return "Pending";
    return raw.charAt(0).toUpperCase() + raw.slice(1);
}

export function formatQueueTime(frame, fps, mode = "frames") {
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

export function formatQueueSelectionSummary(job, options = {}) {
    const start = Math.max(0, parseInt(job?.selection_start, 10) || 0);
    const end = Math.max(start, parseInt(job?.selection_end, 10) || 0);
    const duration = end - start;
    const preContext = Math.max(0, parseInt(job?.pre_context_frames, 10) || 0);
    const postContext = Math.max(0, parseInt(job?.post_context_frames, 10) || 0);
    const maskPre = Math.max(0, parseInt(job?.mask_pre_offset, 10) || 0);
    const maskPost = Math.max(0, parseInt(job?.mask_post_offset, 10) || 0);
    const fps = Math.max(1, Number(job?.scene_fps) || Number(options.fps) || 24);
    const mode = options.mode === "timecode" ? "timecode" : "frames";
    return `In: ${formatQueueTime(start, fps, mode)} Out: ${formatQueueTime(end, fps, mode)} (${formatQueueTime(duration, fps, mode)}) | Ctx: -${preContext}/+${postContext} | Mask Offset: -${maskPre}/+${maskPost}`;
}

export function groupQueueJobs(queue) {
    const groups = [];
    let index = 0;
    const jobs = asArray(queue);
    while (index < jobs.length) {
        const job = jobs[index];
        const batchId = String(job?.batch_id || "");
        if (!batchId) {
            groups.push({ type: "single", job });
            index += 1;
            continue;
        }

        const batchJobs = [job];
        index += 1;
        while (index < jobs.length && String(jobs[index]?.batch_id || "") === batchId) {
            batchJobs.push(jobs[index]);
            index += 1;
        }

        if (batchJobs.length === 1) {
            groups.push({ type: "single", job });
            continue;
        }
        groups.push({ type: "batch", batchId, jobs: batchJobs });
    }
    return groups;
}

export function queueBatchIds(queue) {
    return new Set(
        groupQueueJobs(queue)
            .filter((entry) => entry.type === "batch")
            .map((entry) => entry.batchId)
            .filter(Boolean)
    );
}

export function readQueueBatchCollapseState(projectKey, settings = {}) {
    const key = typeof projectKey === "string" ? projectKey : "";
    const collapsedByProject = settings?.layout?.queueBatchCollapsedByProject;
    const collapsedIds = key && collapsedByProject && typeof collapsedByProject === "object"
        ? collapsedByProject[key]
        : null;
    return asCollapsedSet(collapsedIds);
}

export function persistQueueBatchCollapseState(projectKey, collapsedIds, updateSettings) {
    const key = typeof projectKey === "string" ? projectKey : "";
    if (!key || typeof updateSettings !== "function") return;
    updateSettings({
        layout: {
            queueBatchCollapsedByProject: {
                [key]: Array.from(asCollapsedSet(collapsedIds)).sort(),
            },
        },
    });
}

function queueStatusDotColor(state) {
    if (state === "running") return THEME.statusRunning;
    if (state === "pending") return THEME.statusPending;
    if (state === "failed") return THEME.statusFailed;
    if (state === "completed") return THEME.statusCompleted || THEME.statusRunning;
    return THEME.statusIdle;
}

function makeStatusPill(text, state = "idle", { fontSize = "10px", padding = "2px 8px" } = {}) {
    const pill = style(document.createElement("span"), `
        ${statusPillCss({ state, padding })}
        font-size: ${fontSize};
        line-height: 1.35;
        font-weight: 600;
        flex-shrink: 0;
        white-space: nowrap;
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

function normalizeOptions(options = {}, previous = {}) {
    const surface = options.surface === "dormant" || options.surface === "fullscreen"
        ? options.surface
        : (previous.surface === "dormant" ? "dormant" : "fullscreen");
    const timecodeMode = options.timecodeMode === "timecode" || options.timecodeMode === "frames"
        ? options.timecodeMode
        : (previous.timecodeMode === "timecode" ? "timecode" : "frames");
    return {
        jobs: asArray(options.jobs ?? previous.jobs),
        queueActive: options.queueActive ?? previous.queueActive ?? true,
        surface,
        projectKey: typeof options.projectKey === "string" ? options.projectKey : (previous.projectKey || ""),
        timecodeMode,
        fallbackFps: Math.max(1, Number(options.fallbackFps ?? previous.fallbackFps) || 24),
        collapsedBatchIds: asCollapsedSet(options.collapsedBatchIds ?? previous.collapsedBatchIds),
        emptyText: typeof options.emptyText === "string" ? options.emptyText : (previous.emptyText || "Render queue is empty."),
        showDeleteJob: options.showDeleteJob ?? previous.showDeleteJob ?? false,
        showClearCompleted: options.showClearCompleted ?? previous.showClearCompleted ?? true,
        onSetQueueActive: options.onSetQueueActive ?? previous.onSetQueueActive,
        onDeleteJob: options.onDeleteJob ?? previous.onDeleteJob,
        onClearCompleted: options.onClearCompleted ?? previous.onClearCompleted,
        onBatchCollapsedChange: options.onBatchCollapsedChange ?? previous.onBatchCollapsedChange,
        onAfterAction: options.onAfterAction ?? previous.onAfterAction,
        consumePointer: options.consumePointer ?? previous.consumePointer,
    };
}

function surfaceRootCss(surface) {
    if (surface === "dormant") {
        return `
            display: flex;
            flex-direction: column;
            gap: 6px;
            flex: 1 1 auto;
            min-height: 0;
            height: 100%;
            overflow-y: auto;
            padding-right: 2px;
            box-sizing: border-box;
            font-family: ${FONT.sans};
        `;
    }
    return `
        display: flex;
        flex-direction: column;
        min-height: 0;
        width: 100%;
        box-sizing: border-box;
        font-family: ${FONT.sans};
    `;
}

function callConsume(current, event, options = {}) {
    if (typeof current.consumePointer === "function") {
        current.consumePointer(event, options);
        return;
    }
    if (options.preventDefault) event?.preventDefault?.();
    event?.stopPropagation?.();
}

function bindDormantPointer(current, el, options = {}) {
    if (current.surface !== "dormant" || typeof current.consumePointer !== "function") return;
    el.addEventListener("pointerdown", (event) => current.consumePointer(event, options));
    el.addEventListener("mousedown", (event) => current.consumePointer(event, options));
}

function createActiveControl(current) {
    const row = style(document.createElement("label"), current.surface === "dormant" ? `
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 7px 8px;
        border-radius: 6px;
        background: rgba(255,255,255,0.03);
        border: 1px solid ${CHROME.borderSoft};
        color: ${CHROME.text};
        font-size: 10px;
        font-weight: 700;
        cursor: pointer;
        user-select: none;
    ` : `
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 7px 8px;
        border-bottom: 1px solid ${CHROME.borderSoft};
        color: ${CHROME.textMuted};
        font-size: 10px;
        cursor: pointer;
        user-select: none;
    `);
    row.title = "Toggle whether queued jobs drive editor execution";
    if (current.surface === "dormant" && typeof current.consumePointer === "function") {
        row.addEventListener("pointerdown", (event) => current.consumePointer(event));
        row.addEventListener("mousedown", (event) => current.consumePointer(event));
        row.addEventListener("click", (event) => current.consumePointer(event));
    }

    const label = style(document.createElement("span"), `min-width:0;color:${CHROME.text};font-weight:700;`);
    label.textContent = "Queue Active";
    const right = style(document.createElement("span"), `
        display: flex;
        align-items: center;
        gap: 6px;
        color: ${CHROME.textDim};
        font-size: 10px;
        font-weight: 600;
    `);
    const status = document.createElement("span");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = current.queueActive !== false;
    checkbox.title = row.title;
    checkbox.style.cssText = "margin:0;";
    status.textContent = checkbox.checked ? "On" : "Off";
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", (event) => {
        event.stopPropagation();
        const nextActive = checkbox.checked;
        status.textContent = nextActive ? "On" : "Off";
        current.queueActive = nextActive;
        current.onSetQueueActive?.(nextActive);
    });
    right.append(status, checkbox);
    row.append(label, right);
    return row;
}

function createEmptyState(current) {
    const empty = style(document.createElement("div"), current.surface === "dormant" ? `
        padding: 10px;
        border-radius: 6px;
        background: rgba(255,255,255,0.04);
        border: 1px solid ${CHROME.borderSoft};
        color: ${CHROME.textDim};
        font-size: 10px;
    ` : `
        padding: 10px;
        color: ${CHROME.textMuted};
        font-style: italic;
        font-size: 10px;
    `);
    empty.textContent = current.emptyText;
    return empty;
}

function createActionsRow(current, completedCount) {
    const actions = style(document.createElement("div"), current.surface === "dormant" ? `
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: 6px;
        padding-bottom: 2px;
    ` : `
        display: flex;
        justify-content: flex-end;
        align-items: center;
        gap: 6px;
        padding: 6px 8px;
        border-bottom: 1px solid ${CHROME.borderSoft};
        background: ${CHROME.panelMuted};
    `);
    const clearBtn = style(document.createElement("button"), chromeButtonCss({
        variant: "subtle",
        padding: current.surface === "dormant" ? "4px 7px" : "4px 8px",
        radius: "6px",
        fontSize: "10px",
        fontWeight: "700",
    }));
    clearBtn.type = "button";
    clearBtn.textContent = "Clear Completed Renders";
    clearBtn.title = `Remove ${completedCount} completed render${completedCount === 1 ? "" : "s"} from the queue`;
    bindDormantPointer(current, clearBtn, { preventDefault: true });
    clearBtn.addEventListener("click", async (event) => {
        callConsume(current, event, { preventDefault: true });
        clearBtn.disabled = true;
        clearBtn.style.opacity = "0.65";
        try {
            await current.onClearCompleted?.();
            await current.onAfterAction?.({ type: "clear-completed" });
        } catch (error) {
            clearBtn.disabled = false;
            clearBtn.style.opacity = "";
            console.warn("[Sonder] Failed to clear completed renders:", error);
        }
    });
    actions.appendChild(clearBtn);
    return actions;
}

function createQueueRow(current, job, { title = "", nested = false } = {}) {
    const statusState = statusStateForQueue(job?.status);
    const showDelete = !!current.showDeleteJob && typeof current.onDeleteJob === "function";
    const row = style(document.createElement("div"), current.surface === "dormant" ? `
        display: grid;
        grid-template-columns: auto minmax(0, 1fr);
        gap: 8px;
        padding: 7px 8px${nested ? " 7px 18px" : ""};
        border-radius: 6px;
        background: ${nested ? "rgba(255,255,255,0.02)" : "rgba(255,255,255,0.03)"};
        border: 1px solid ${CHROME.borderSoft};
        align-items: start;
        color: ${CHROME.text};
    ` : `
        padding: 6px 8px${nested ? " 6px 18px" : ""};
        border-bottom: 1px solid ${CHROME.borderSoft};
        display: grid;
        grid-template-columns: auto minmax(0, 1fr) ${showDelete ? "auto" : ""};
        gap: 8px;
        font-size: 10px;
        color: ${CHROME.text};
        background: ${nested ? CHROME.panel : CHROME.panelMuted};
        align-items: start;
    `);

    const dot = style(document.createElement("span"), `
        width: 8px;
        height: 8px;
        margin-top: 4px;
        border-radius: 50%;
        background: ${queueStatusDotColor(statusState)};
        flex-shrink: 0;
    `);
    dot.title = formatQueueStatusLabel(job?.status);
    row.appendChild(dot);

    const text = style(document.createElement("div"), `
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 2px;
    `);
    if (job?.prompt) text.title = job.prompt;

    const headingRow = style(document.createElement("div"), `
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 8px;
        min-width: 0;
    `);
    const heading = style(document.createElement("div"), `
        color: ${CHROME.text};
        font-size: 11px;
        font-weight: 600;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        min-width: 0;
    `);
    heading.textContent = title || job?.scene_name || "Scene";
    const status = makeStatusPill(formatQueueStatusLabel(job?.status), statusState, {
        fontSize: "10px",
        padding: "1px 7px",
    });

    const selectionSummary = style(document.createElement("div"), `
        color: ${CHROME.textDim};
        font-size: 10px;
        line-height: 1.35;
        white-space: normal;
        overflow-wrap: anywhere;
    `);
    selectionSummary.textContent = formatQueueSelectionSummary(job, {
        fps: current.fallbackFps,
        mode: current.timecodeMode,
    });

    headingRow.append(heading, status);
    text.append(headingRow, selectionSummary);
    row.appendChild(text);

    if (showDelete) {
        const delBtn = style(document.createElement("button"), `
            appearance: none;
            border: none;
            background: transparent;
            color: ${CHROME.textMuted};
            cursor: pointer;
            padding: 0 2px;
            font-size: 11px;
            line-height: 1;
        `);
        delBtn.type = "button";
        delBtn.textContent = "x";
        delBtn.title = "Delete queue job";
        delBtn.addEventListener("click", async (event) => {
            event.preventDefault();
            event.stopPropagation();
            delBtn.disabled = true;
            try {
                await current.onDeleteJob(job);
                await current.onAfterAction?.({ type: "delete-job", job });
            } catch (error) {
                delBtn.disabled = false;
                console.warn("[Sonder] Failed to delete queue job:", error);
            }
        });
        row.appendChild(delBtn);
    }

    return row;
}

function batchCountLabel(entry) {
    const batchTotal = Math.max(
        entry.jobs.length,
        ...entry.jobs.map((job) => Math.max(0, parseInt(job?.batch_total, 10) || 0))
    );
    const countLabel = entry.jobs.length === batchTotal
        ? `${batchTotal} chunk${batchTotal === 1 ? "" : "s"}`
        : `${entry.jobs.length} of ${batchTotal} chunks`;
    return { batchTotal, countLabel };
}

function createBatchGroup(current, entry, rerender) {
    const { batchTotal, countLabel } = batchCountLabel(entry);
    const isOpen = !current.collapsedBatchIds.has(entry.batchId);
    const group = style(document.createElement("div"), current.surface === "dormant" ? `
        display: flex;
        flex-direction: column;
        gap: 4px;
        padding: 0;
        border-radius: 6px;
        background: rgba(255,255,255,0.02);
        border: 1px solid ${CHROME.borderSoft};
    ` : `
        border-bottom: 1px solid ${CHROME.borderSoft};
        background: ${CHROME.panelMuted};
    `);

    const header = style(document.createElement("button"), current.surface === "dormant" ? `
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        width: 100%;
        padding: 7px 8px;
        background: rgba(255,255,255,0.03);
        border-top: none;
        border-left: none;
        border-right: none;
        border-bottom: 1px solid ${CHROME.borderSoft};
        border-bottom-left-radius: 0;
        border-bottom-right-radius: 0;
        color: ${CHROME.text};
        font-size: 10px;
        font-weight: 700;
        cursor: pointer;
        text-align: left;
    ` : `
        width: 100%;
        padding: 6px 8px;
        background: ${CHROME.panelMuted};
        border: none;
        border-bottom: 1px solid ${CHROME.borderSoft};
        color: ${CHROME.text};
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        cursor: pointer;
        font-size: 10px;
        font-weight: 700;
        text-align: left;
    `);
    header.type = "button";
    header.setAttribute("aria-expanded", isOpen ? "true" : "false");
    bindDormantPointer(current, header, { preventDefault: true });

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
    header.addEventListener("click", (event) => {
        callConsume(current, event, { preventDefault: true });
        const nextCollapsed = new Set(current.collapsedBatchIds);
        if (isOpen) {
            nextCollapsed.add(entry.batchId);
        } else {
            nextCollapsed.delete(entry.batchId);
        }
        current.collapsedBatchIds = nextCollapsed;
        current.onBatchCollapsedChange?.(new Set(nextCollapsed));
        rerender();
    });
    group.appendChild(header);

    if (isOpen) {
        const rows = style(document.createElement("div"), current.surface === "dormant" ? `
            display: flex;
            flex-direction: column;
            gap: 4px;
            padding: 0 0 4px;
        ` : `
            display: flex;
            flex-direction: column;
        `);
        entry.jobs.forEach((job, index) => {
            const chunkIndex = Math.max(1, (parseInt(job?.batch_index, 10) || index) + 1);
            rows.appendChild(createQueueRow(current, job, {
                title: `Chunk ${chunkIndex} of ${batchTotal}`,
                nested: true,
            }));
        });
        group.appendChild(rows);
    }

    return group;
}

export function mountSharedRenderQueue(container, options = {}) {
    let current = normalizeOptions(options);
    let destroyed = false;
    const root = style(document.createElement("div"), surfaceRootCss(current.surface));
    container.appendChild(root);

    const render = () => {
        if (destroyed) return;
        root.style.cssText = surfaceRootCss(current.surface);
        root.innerHTML = "";
        root.appendChild(createActiveControl(current));
        const jobs = asArray(current.jobs);
        if (!jobs.length) {
            root.appendChild(createEmptyState(current));
            return;
        }
        const completedCount = jobs.filter((job) => String(job?.status || "").toLowerCase() === "completed").length;
        if (current.showClearCompleted !== false && completedCount > 0 && typeof current.onClearCompleted === "function") {
            root.appendChild(createActionsRow(current, completedCount));
        }
        for (const entry of groupQueueJobs(jobs)) {
            if (entry.type === "single") {
                root.appendChild(createQueueRow(current, entry.job));
            } else {
                root.appendChild(createBatchGroup(current, entry, render));
            }
        }
    };

    render();

    return {
        root,
        update(nextOptions = {}) {
            current = normalizeOptions(nextOptions, current);
            render();
        },
        destroy() {
            destroyed = true;
            root.remove();
        },
    };
}
