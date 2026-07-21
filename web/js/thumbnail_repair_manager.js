const { api } = window.comfyAPI.api;

import { notifyProgress } from "./editor_notifications.js";

export const AUTOMATIC_THUMBNAIL_QUIET_MS = 1200;
export const AUTOMATIC_THUMBNAIL_YIELD_MS = 250;
export const BULK_THUMBNAIL_CONCURRENCY = 2;

const REPAIRABLE_ASSET_TYPES = new Set(["video", "image", "audio"]);
const automaticQueue = new Map();
const automaticOwnerKeys = new Map();
const automaticFailures = new Set();
const inFlightRepairs = new Map();
const stateListeners = new Set();
const repairListeners = new Set();

let automaticTimer = null;
let automaticRunning = false;
let bulkJob = null;
let executingNodeId = null;
let queueRemaining = 0;
let bulkSequence = 0;

function projectIdFromDir(projectDir) {
    return String(projectDir || "").split(/[/\\]/).filter(Boolean).pop() || "";
}

function assetSignature(asset) {
    return String(
        asset?.media_probe_signature
        || `${asset?.path || ""}|${asset?.size_bytes || 0}|${asset?.imported_at || ""}`,
    );
}

function repairKey(projectDir, asset) {
    return `${projectIdFromDir(projectDir)}|${asset?.asset_id || ""}|${assetSignature(asset)}`;
}

export function isThumbnailRepairCandidate(asset) {
    return !!(
        asset
        && asset.asset_id
        && REPAIRABLE_ASSET_TYPES.has(asset.asset_type)
        && !asset.has_thumbnail
        && !asset.missing
        && !asset.trashed_at
    );
}

function thumbnailUrl(projectDir, assetId) {
    const projectId = encodeURIComponent(projectIdFromDir(projectDir));
    return api.apiURL(`/sonder-editor/project/${projectId}/thumbnail/${encodeURIComponent(assetId)}`);
}

function automaticBusy() {
    return executingNodeId != null || queueRemaining > 0;
}

function publicBulkState() {
    if (!bulkJob) return null;
    return {
        id: bulkJob.id,
        ownerId: bulkJob.ownerId,
        projectId: bulkJob.projectId,
        total: bulkJob.total,
        completed: bulkJob.completed,
        repaired: bulkJob.repaired,
        failed: bulkJob.failed,
        cancelRequested: bulkJob.cancelRequested,
    };
}

function emitState() {
    const snapshot = publicBulkState();
    for (const listener of stateListeners) {
        try { listener(snapshot); } catch (error) { console.warn("[Sonder] Thumbnail repair listener failed:", error); }
    }
}

function emitRepair(projectDir, asset) {
    const payload = {
        projectId: projectIdFromDir(projectDir),
        assetId: asset.asset_id,
        signature: assetSignature(asset),
    };
    for (const listener of repairListeners) {
        try { listener(payload); } catch (error) { console.warn("[Sonder] Thumbnail repaired listener failed:", error); }
    }
}

async function requestThumbnail(projectDir, asset) {
    const key = repairKey(projectDir, asset);
    const existing = inFlightRepairs.get(key);
    if (existing) return existing;

    const promise = (async () => {
        const response = await fetch(thumbnailUrl(projectDir, asset.asset_id));
        if (!response.ok) {
            throw new Error(`Thumbnail generation failed: ${response.status}`);
        }
        if (typeof response.arrayBuffer === "function") {
            await response.arrayBuffer();
        }
        asset.has_thumbnail = true;
        emitRepair(projectDir, asset);
        return true;
    })().finally(() => {
        if (inFlightRepairs.get(key) === promise) inFlightRepairs.delete(key);
    });
    inFlightRepairs.set(key, promise);
    return promise;
}

function clearAutomaticTimer() {
    if (automaticTimer == null) return;
    (window.clearTimeout || clearTimeout)(automaticTimer);
    automaticTimer = null;
}

function scheduleAutomaticPump(delay = AUTOMATIC_THUMBNAIL_QUIET_MS, { reset = false } = {}) {
    if (reset) clearAutomaticTimer();
    if (automaticTimer != null || automaticRunning || !automaticQueue.size) return;
    automaticTimer = (window.setTimeout || setTimeout)(() => {
        automaticTimer = null;
        void pumpAutomaticQueue();
    }, delay);
}

function forgetAutomaticTask(key) {
    const task = automaticQueue.get(key);
    if (!task) return;
    automaticQueue.delete(key);
    for (const ownerId of task.owners) {
        const keys = automaticOwnerKeys.get(ownerId);
        keys?.delete(key);
        if (keys && !keys.size) automaticOwnerKeys.delete(ownerId);
    }
}

async function pumpAutomaticQueue() {
    if (automaticRunning || bulkJob || automaticBusy()) return;
    if (typeof document !== "undefined" && document.visibilityState === "hidden") return;

    let task = null;
    for (const candidate of automaticQueue.values()) {
        if (candidate.owners.size && isThumbnailRepairCandidate(candidate.asset)) {
            task = candidate;
            break;
        }
        forgetAutomaticTask(candidate.key);
    }
    if (!task) return;

    automaticRunning = true;
    try {
        await requestThumbnail(task.projectDir, task.asset);
    } catch (_error) {
        automaticFailures.add(task.key);
    } finally {
        forgetAutomaticTask(task.key);
        automaticRunning = false;
        scheduleAutomaticPump(AUTOMATIC_THUMBNAIL_YIELD_MS);
    }
}

export function enqueueAutomaticThumbnailRepair({ ownerId, projectDir, asset } = {}) {
    const normalizedOwner = String(ownerId || "");
    const key = repairKey(projectDir, asset);
    if (!normalizedOwner || !projectIdFromDir(projectDir) || !isThumbnailRepairCandidate(asset)) return false;
    if (automaticFailures.has(key)) return false;

    let task = automaticQueue.get(key);
    if (!task) {
        task = { key, projectDir, asset, owners: new Set() };
        automaticQueue.set(key, task);
    }
    task.owners.add(normalizedOwner);
    if (!automaticOwnerKeys.has(normalizedOwner)) automaticOwnerKeys.set(normalizedOwner, new Set());
    automaticOwnerKeys.get(normalizedOwner).add(key);
    scheduleAutomaticPump(AUTOMATIC_THUMBNAIL_QUIET_MS, { reset: true });
    return true;
}

export function cancelAutomaticThumbnailRepairs(ownerId) {
    const normalizedOwner = String(ownerId || "");
    const keys = automaticOwnerKeys.get(normalizedOwner);
    if (!keys) return;
    for (const key of Array.from(keys)) {
        const task = automaticQueue.get(key);
        task?.owners.delete(normalizedOwner);
        if (task && !task.owners.size) automaticQueue.delete(key);
    }
    automaticOwnerKeys.delete(normalizedOwner);
    if (!automaticQueue.size) clearAutomaticTimer();
}

export async function fetchMissingThumbnailAssets(projectDir) {
    const projectId = encodeURIComponent(projectIdFromDir(projectDir));
    if (!projectId) throw new Error("Open a project before regenerating thumbnails.");
    const response = await fetch(api.apiURL(`/sonder-editor/project/${projectId}/assets?include_trashed=true`));
    if (!response.ok) throw new Error(`Failed to inspect thumbnails: ${response.status}`);
    const payload = await response.json();
    return (payload?.assets || []).filter(isThumbnailRepairCandidate);
}

function bulkProgressMessage(job) {
    return `${job.completed}/${job.total} complete · ${job.repaired} repaired${job.failed ? ` · ${job.failed} failed` : ""}`;
}

function updateBulkProgress(job) {
    job.notification?.update({
        message: bulkProgressMessage(job),
        progress: { current: job.completed, total: job.total, unit: "" },
    });
    emitState();
}

async function runBulkJob(job) {
    let nextIndex = 0;
    const worker = async () => {
        while (!job.cancelRequested) {
            const index = nextIndex;
            nextIndex += 1;
            if (index >= job.assets.length) return;
            const asset = job.assets[index];
            try {
                await requestThumbnail(job.projectDir, asset);
                job.repaired += 1;
            } catch (error) {
                job.failed += 1;
                console.warn("[Sonder] Thumbnail regeneration failed:", asset?.asset_id, error);
            }
            job.completed += 1;
            updateBulkProgress(job);
        }
    };

    await Promise.all(Array.from({ length: Math.min(BULK_THUMBNAIL_CONCURRENCY, job.total) }, worker));

    const result = {
        projectId: job.projectId,
        total: job.total,
        completed: job.completed,
        repaired: job.repaired,
        failed: job.failed,
        cancelled: job.cancelRequested,
    };
    if (job.cancelRequested) {
        job.notification?.resolve({
            tier: "warning",
            message: `Thumbnail regeneration stopped after ${job.completed}/${job.total}. ${job.repaired} repaired${job.failed ? `, ${job.failed} failed` : ""}.`,
        });
    } else if (!job.failed) {
        job.notification?.resolve({ message: `Regenerated ${job.repaired} thumbnail${job.repaired === 1 ? "" : "s"}.` });
    } else if (job.repaired) {
        job.notification?.resolve({ tier: "warning", message: `Regenerated ${job.repaired} of ${job.total} thumbnails; ${job.failed} failed.` });
    } else {
        job.notification?.resolve({ tier: "error", message: `Thumbnail regeneration failed for all ${job.total} assets.` });
    }

    if (bulkJob === job) bulkJob = null;
    emitState();
    scheduleAutomaticPump(AUTOMATIC_THUMBNAIL_YIELD_MS);
    return result;
}

export function startBulkThumbnailRepair({ ownerId, projectDir, assets } = {}) {
    if (bulkJob) throw new Error(`Thumbnail regeneration is already running for ${bulkJob.projectId}.`);
    const unique = new Map();
    for (const asset of (assets || [])) {
        if (isThumbnailRepairCandidate(asset)) unique.set(repairKey(projectDir, asset), asset);
    }
    const candidates = Array.from(unique.values());
    if (!candidates.length) throw new Error("No missing thumbnails need regeneration.");

    const projectId = projectIdFromDir(projectDir);
    const job = {
        id: `thumbnail-bulk-${++bulkSequence}`,
        ownerId: String(ownerId || ""),
        projectId,
        projectDir,
        assets: candidates,
        total: candidates.length,
        completed: 0,
        repaired: 0,
        failed: 0,
        cancelRequested: false,
        notification: null,
        promise: null,
    };
    bulkJob = job;
    clearAutomaticTimer();
    job.notification = notifyProgress({
        verb: "Regenerating thumbnails",
        message: bulkProgressMessage(job),
        progress: { current: 0, total: job.total, unit: "" },
        foreground: true,
        source: `thumbnail-repair:${projectId}`,
        actions: [{
            label: "Cancel",
            variant: "warning",
            dismiss: false,
            fn: () => cancelBulkThumbnailRepair({ ownerId: job.ownerId }),
        }],
    });
    emitState();
    job.promise = runBulkJob(job);
    return job.promise;
}

export function cancelBulkThumbnailRepair({ ownerId = "", projectId = "" } = {}) {
    if (!bulkJob) return false;
    if (ownerId && bulkJob.ownerId !== ownerId) return false;
    if (projectId && bulkJob.projectId !== projectIdFromDir(projectId)) return false;
    if (bulkJob.cancelRequested) return true;
    bulkJob.cancelRequested = true;
    bulkJob.notification?.update({
        message: `Stopping after active thumbnail generation finishes · ${bulkJob.completed}/${bulkJob.total} complete`,
        actions: [],
    });
    emitState();
    return true;
}

export function cancelThumbnailRepairOwner(ownerId) {
    cancelAutomaticThumbnailRepairs(ownerId);
    cancelBulkThumbnailRepair({ ownerId });
}

export function getBulkThumbnailRepairState() {
    return publicBulkState();
}

export function subscribeThumbnailRepairState(listener) {
    stateListeners.add(listener);
    try { listener(publicBulkState()); } catch (_error) {}
    return () => stateListeners.delete(listener);
}

export function subscribeThumbnailRepairs(listener) {
    repairListeners.add(listener);
    return () => repairListeners.delete(listener);
}

function handleStatus(event) {
    const remaining = Number(event?.detail?.exec_info?.queue_remaining);
    if (Number.isFinite(remaining)) queueRemaining = Math.max(0, remaining);
    if (!automaticBusy()) scheduleAutomaticPump(AUTOMATIC_THUMBNAIL_YIELD_MS);
}

function handleExecuting(event) {
    const detail = event?.detail;
    executingNodeId = (detail && typeof detail === "object") ? (detail.node ?? null) : (detail ?? null);
    if (!automaticBusy()) scheduleAutomaticPump(AUTOMATIC_THUMBNAIL_YIELD_MS);
}

if (typeof api?.addEventListener === "function") {
    api.addEventListener("status", handleStatus);
    api.addEventListener("executing", handleExecuting);
}

if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState !== "hidden") scheduleAutomaticPump(AUTOMATIC_THUMBNAIL_YIELD_MS);
    });
}

export function _resetThumbnailRepairManagerForTests() {
    clearAutomaticTimer();
    automaticQueue.clear();
    automaticOwnerKeys.clear();
    automaticFailures.clear();
    inFlightRepairs.clear();
    stateListeners.clear();
    repairListeners.clear();
    automaticRunning = false;
    bulkJob = null;
    executingNodeId = null;
    queueRemaining = 0;
    bulkSequence = 0;
}
