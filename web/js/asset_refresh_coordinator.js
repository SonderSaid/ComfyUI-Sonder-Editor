import {
    createStaleReplayGovernor,
    fetchProjectJson,
    getProjectVersion,
    postProjectJsonWithReconcile,
    resetProjectVersion,
} from "./api_client.js";

const RETRY_DELAYS_MS = [250, 1000, 4000];
const EXHAUSTED_WAVE_TTL_MS = 60_000;
const MODE_RANK = { read: 0, sync: 1 };

let waveSequence = 0;
let requestSequence = 0;

function normalizedProjectId(projectId) {
    return String(projectId || "").trim();
}

function normalizedMode(mode) {
    return mode === "sync" ? "sync" : "read";
}

function maxVersion(...versions) {
    return versions
        .map((value) => String(value || ""))
        .filter(Boolean)
        .sort()
        .at(-1) || "";
}

function responseVersion(response, payload) {
    return String(
        response?.headers?.get?.("X-Sonder-Project-Modified-At")
        || payload?.modified_at
        || payload?.project?.modified_at
        || "",
    );
}

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((onResolve, onReject) => {
        resolve = onResolve;
        reject = onReject;
    });
    return { promise, resolve, reject };
}

function waitWithCancellation(delayMs, state) {
    return new Promise((resolve) => {
        const timer = setTimeout(() => {
            if (state.retryWait?.timer === timer) state.retryWait = null;
            resolve(true);
        }, delayMs);
        state.retryWait = {
            timer,
            cancel() {
                clearTimeout(timer);
                if (state.retryWait?.timer === timer) state.retryWait = null;
                resolve(false);
            },
        };
    });
}

function policyFromInput(policy = {}) {
    const includeTrashed = policy.includeTrashed !== false;
    const retentionDays = Number.isFinite(Number(policy.retentionDays))
        ? Math.max(0, Math.round(Number(policy.retentionDays)))
        : 5;
    const rawMaxSize = policy.maxSizeMB;
    const maxSizeMB = rawMaxSize === null || rawMaxSize === undefined || rawMaxSize === ""
        ? null
        : (Number.isFinite(Number(rawMaxSize)) ? Math.max(0, Number(rawMaxSize)) : null);
    return { includeTrashed, retentionDays, maxSizeMB };
}

export function buildAssetRefreshPolicy(settings = {}) {
    return policyFromInput({
        includeTrashed: true,
        retentionDays: settings?.render?.trashRetentionDays,
        maxSizeMB: settings?.render?.trashMaxSizeMB,
    });
}

export function assetRefreshPolicyQuery(policy = {}) {
    const normalized = policyFromInput(policy);
    const params = new URLSearchParams();
    params.set("include_trashed", normalized.includeTrashed ? "true" : "false");
    params.set("retention_days", String(normalized.retentionDays));
    if (normalized.maxSizeMB !== null) {
        params.set("max_size_mb", String(normalized.maxSizeMB));
    }
    return params.toString();
}

function policySignature(policy) {
    return assetRefreshPolicyQuery(policy);
}

function sanitizedEvent(kind, state, demand, details = {}) {
    return {
        kind,
        request_id: demand?.requestId || "",
        wave_id: demand?.waveId || "",
        project_id: state.projectId,
        mode: demand?.mode || "",
        policy_signature: demand?.policySignature || "",
        required_version: demand?.requiredVersion || "",
        mutation_epoch: demand?.epoch ?? state.mutationEpoch,
        reason: demand?.reason || "",
        t_wall: Date.now(),
        ...details,
    };
}

function emit(state, demand, kind, details = {}) {
    const event = sanitizedEvent(kind, state, demand, details);
    for (const recorder of demand?.recorders || []) {
        try { recorder(event); } catch (_) {}
    }
    if (typeof window !== "undefined" && window.SONDER_DEBUG_SESSION === true) {
        if (!Array.isArray(window.__SONDER_ASSET_REFRESH_DIAG)) {
            window.__SONDER_ASSET_REFRESH_DIAG = [];
        }
        window.__SONDER_ASSET_REFRESH_DIAG.push(event);
        if (window.__SONDER_ASSET_REFRESH_DIAG.length > 2048) {
            window.__SONDER_ASSET_REFRESH_DIAG.splice(
                0,
                window.__SONDER_ASSET_REFRESH_DIAG.length - 2048,
            );
        }
    }
}

function demandCanJoin(active, incoming) {
    if (active.waveId !== incoming.waveId) return false;
    if (active.epoch !== incoming.epoch) return false;
    if (MODE_RANK[active.mode] < MODE_RANK[incoming.mode]) return false;
    if (active.mode === "sync" && active.policySignature !== incoming.policySignature) return false;
    return !incoming.requiredVersion
        || (!!active.requiredVersion && active.requiredVersion >= incoming.requiredVersion);
}

function mergeDemand(target, incoming) {
    if (!target) return incoming;
    target.waveId = incoming.waveId;
    target.unknownVersion = incoming.unknownVersion;
    target.requiredVersion = maxVersion(target.requiredVersion, incoming.requiredVersion);
    target.epoch = Math.max(target.epoch, incoming.epoch);
    if (MODE_RANK[incoming.mode] > MODE_RANK[target.mode]) {
        target.mode = incoming.mode;
        target.policy = incoming.policy;
        target.policySignature = incoming.policySignature;
    } else if (incoming.mode === "sync" && target.policySignature !== incoming.policySignature) {
        target.policy = incoming.policy;
        target.policySignature = incoming.policySignature;
    }
    target.manual = target.manual || incoming.manual;
    target.reason = incoming.reason || target.reason;
    for (const recorder of incoming.recorders) target.recorders.add(recorder);
    target.waiters.push(...incoming.waiters);
    return target;
}

function resultSatisfiesDemand(result, demand, state) {
    if (!result || result.error) return false;
    if (result.epoch < demand.epoch || result.epoch < state.mutationEpoch) return false;
    if (MODE_RANK[result.mode] < MODE_RANK[demand.mode]) return false;
    if (demand.mode === "sync" && result.policySignature !== demand.policySignature) return false;
    if (result.waveId !== demand.waveId && demand.unknownVersion) return false;
    return !demand.requiredVersion
        || (!!result.servedVersion && result.servedVersion >= demand.requiredVersion);
}

function settleWaiters(demand, result, error = null) {
    for (const waiter of demand?.waiters || []) {
        if (error) waiter.reject(error);
        else waiter.resolve(result);
    }
    demand.waiters.length = 0;
}

function pruneExhausted(state, now = Date.now()) {
    for (const [waveId, exhausted] of state.exhaustedWaves.entries()) {
        if (now - exhausted.at >= EXHAUSTED_WAVE_TTL_MS) {
            state.exhaustedWaves.delete(waveId);
        }
    }
}

async function defaultRequest(demand) {
    const api = window.comfyAPI.api.api;
    const query = demand.policySignature;
    const url = api.apiURL(
        `/sonder-editor/project/${encodeURIComponent(demand.projectId)}/assets${demand.mode === "sync" ? "/sync" : ""}?${query}`,
    );
    const result = demand.mode === "sync"
        ? await postProjectJsonWithReconcile(
            url,
            { method: "POST" },
            { projectId: demand.projectId },
        )
        : await fetchProjectJson(url, {}, { projectId: demand.projectId });
    return result;
}

// Detail-only transport. Never returns or implies a list: a detail response carries
// per-asset revisions and the version it was read at, nothing a list consumer could adopt.
const DETAIL_ROUTES = { provenance: "provenance", search: "search-metadata" };

async function defaultDetailRequest({ projectId, kind, assetIds }) {
    const api = window.comfyAPI.api.api;
    const query = new URLSearchParams();
    for (const id of assetIds) query.append("asset_id", id);
    const { payload } = await fetchProjectJson(api.apiURL(
        `/sonder-editor/project/${encodeURIComponent(projectId)}/assets/${DETAIL_ROUTES[kind]}?${query}`),
        {}, { projectId });
    return {
        modifiedAt: String(payload?.modified_at || ""),
        revisions: payload?.revisions || {},
        values: (kind === "search" ? payload?.tracked_metadata : payload?.provenance) || {},
    };
}

// Lower rank dispatches first. A batch holds one kind and one rank, so a selected asset
// is never made to wait for a preload window's component reads in the same request.
export const ASSET_DETAIL_PRIORITY = Object.freeze({ selected: 0, search: 1, prefetch: 2, background: 3 });
export const ASSET_DETAIL_BATCH_LIMIT = 64;
const DETAIL_PRIORITY_NAMES = Object.keys(ASSET_DETAIL_PRIORITY)
    .sort((a, b) => ASSET_DETAIL_PRIORITY[a] - ASSET_DETAIL_PRIORITY[b]);
const DETAIL_SUPERSEDE_REQUEUE_LIMIT = 2;

function detailRank(priority) {
    return Object.prototype.hasOwnProperty.call(ASSET_DETAIL_PRIORITY, priority)
        ? ASSET_DETAIL_PRIORITY[priority]
        : ASSET_DETAIL_PRIORITY.background;
}

export function createAssetRefreshCoordinator({
    request = defaultRequest,
    detailRequest = defaultDetailRequest,
    getLiveVersion = getProjectVersion,
    resetVersion = resetProjectVersion,
    retryDelaysMs = RETRY_DELAYS_MS,
    waitForRetry = waitWithCancellation,
} = {}) {
    const states = new Map();
    const mutationEpochs = new Map();

    const stateFor = (projectId) => {
        const normalized = normalizedProjectId(projectId);
        let state = states.get(normalized);
        if (!state) {
            state = {
                projectId: normalized,
                mutationEpoch: mutationEpochs.get(normalized) || 0,
                active: null,
                pending: null,
                retryWait: null,
                staleGovernor: createStaleReplayGovernor(),
                exhaustedWaves: new Map(),
                exhaustedCleanupTimer: null,
            };
            states.set(normalized, state);
        }
        pruneExhausted(state);
        return state;
    };

    const scheduleExhaustedCleanup = (state) => {
        if (state.exhaustedCleanupTimer) return;
        state.exhaustedCleanupTimer = setTimeout(() => {
            state.exhaustedCleanupTimer = null;
            pruneExhausted(state);
            if (!state.active && !state.pending && !state.exhaustedWaves.size) {
                states.delete(state.projectId);
            } else if (state.exhaustedWaves.size) {
                scheduleExhaustedCleanup(state);
            }
        }, EXHAUSTED_WAVE_TTL_MS);
        state.exhaustedCleanupTimer?.unref?.();
    };

    const execute = async (state, demand) => {
        const startedAt = performance.now();
        let transportFailures = 0;
        let networkAttempts = 0;
        while (true) {
            networkAttempts += 1;
            emit(state, demand, "asset_refresh_request", { attempt: networkAttempts });
            let raw;
            try {
                raw = await request(demand);
            } catch (error) {
                transportFailures += 1;
                emit(state, demand, "asset_refresh_error", {
                    attempt: networkAttempts,
                    status: Number(error?.status) || 0,
                    error_code: String(error?.code || error?.name || "error"),
                    duration_ms: performance.now() - startedAt,
                });
                if (demand.mode !== "read" || demand.manual || transportFailures > retryDelaysMs.length) {
                    state.exhaustedWaves.set(demand.waveId, { at: Date.now(), error });
                    scheduleExhaustedCleanup(state);
                    throw error;
                }
                const delayMs = retryDelaysMs[transportFailures - 1];
                emit(state, demand, "asset_refresh_backoff", {
                    attempt: networkAttempts,
                    delay_ms: delayMs,
                    cause: "transport",
                });
                const shouldContinue = await waitForRetry(delayMs, state);
                if (!shouldContinue) {
                    const superseded = new Error("Asset refresh retry superseded");
                    superseded.code = "asset_refresh_superseded";
                    throw superseded;
                }
                continue;
            }

            const payload = raw?.payload ?? raw;
            const response = raw?.response || null;
            const servedVersion = responseVersion(response, payload);
            const requiredVersion = maxVersion(
                demand.requiredVersion,
                getLiveVersion(state.projectId),
            );
            if (servedVersion && requiredVersion && servedVersion < requiredVersion) {
                const decision = state.staleGovernor.reject(state.projectId, servedVersion);
                emit(state, demand, "asset_refresh_stale_rejection", {
                    attempt: networkAttempts,
                    served_version: servedVersion,
                    live_version: requiredVersion,
                    rejection_count: decision.rejectionCount,
                    action: decision.action,
                });
                if (decision.action === "retry") {
                    emit(state, demand, "asset_refresh_backoff", {
                        attempt: networkAttempts,
                        delay_ms: decision.delayMs,
                        cause: "stale_version",
                    });
                    const shouldContinue = await waitForRetry(decision.delayMs, state);
                    if (!shouldContinue) {
                        const superseded = new Error("Asset refresh retry superseded");
                        superseded.code = "asset_refresh_superseded";
                        throw superseded;
                    }
                    continue;
                }
                resetVersion(state.projectId, servedVersion);
            } else {
                state.staleGovernor.reset(state.projectId);
            }

            const result = {
                payload,
                response,
                projectId: state.projectId,
                mode: demand.mode,
                waveId: demand.waveId,
                epoch: demand.epoch,
                policySignature: demand.policySignature,
                requestId: demand.requestId,
                servedVersion,
                attempts: networkAttempts,
            };
            emit(state, demand, "asset_refresh_response", {
                attempt: networkAttempts,
                served_version: servedVersion,
                status: Number(response?.status) || 200,
                duration_ms: performance.now() - startedAt,
                asset_count: Array.isArray(payload?.assets) ? payload.assets.length : 0,
            });
            return result;
        }
    };

    const drain = (state, demand) => {
        state.active = demand;
        demand.requestId = `asset-${++requestSequence}`;
        const run = execute(state, demand);
        demand.runPromise = run;
        run.then((result) => {
            if (state.active !== demand) return;
            state.active = null;
            const superseded = result.epoch < state.mutationEpoch;
            if (superseded) {
                emit(state, demand, "asset_refresh_supersede", {
                    response_epoch: result.epoch,
                    live_epoch: state.mutationEpoch,
                });
            }
            if (state.pending && !superseded && resultSatisfiesDemand(result, state.pending, state)) {
                const pending = state.pending;
                state.pending = null;
                emit(state, pending, "asset_refresh_followup_collapsed", {
                    satisfied_by_request_id: result.requestId,
                    served_version: result.servedVersion,
                });
                settleWaiters(pending, result);
            }
            if (!superseded) settleWaiters(demand, result);
            if (state.pending) {
                const pending = state.pending;
                state.pending = null;
                emit(state, pending, "asset_refresh_followup", {
                    previous_request_id: result.requestId,
                });
                drain(state, pending);
            } else if (!state.exhaustedWaves.size) {
                states.delete(state.projectId);
            }
        }).catch((error) => {
            if (state.active !== demand) return;
            state.active = null;
            if (error?.code !== "asset_refresh_superseded") {
                settleWaiters(demand, null, error);
            }
            if (state.pending) {
                const pending = state.pending;
                state.pending = null;
                drain(state, pending);
            } else if (!state.exhaustedWaves.size) {
                states.delete(state.projectId);
            }
        });
    };

    const requestRefresh = (input = {}) => {
        const projectId = normalizedProjectId(input.projectId);
        if (!projectId) return Promise.resolve(null);
        const state = stateFor(projectId);
        const mode = normalizedMode(input.mode);
        const waveId = String(input.waveId || allocateAssetRefreshWave(input.reason || mode));
        const exhausted = state.exhaustedWaves.get(waveId);
        if (exhausted && !input.manual) {
            return Promise.reject(exhausted.error);
        }
        const waiter = deferred();
        const policy = policyFromInput(input.policy);
        const demand = {
            projectId,
            mode,
            waveId,
            requiredVersion: maxVersion(
                String(input.requiredVersion || ""),
                getLiveVersion(projectId),
            ),
            unknownVersion: !String(input.requiredVersion || ""),
            epoch: state.mutationEpoch,
            policy,
            policySignature: policySignature(policy),
            manual: !!input.manual,
            reason: String(input.reason || mode),
            requestId: "",
            recorders: new Set(typeof input.diagnosticRecorder === "function"
                ? [input.diagnosticRecorder]
                : []),
            waiters: [waiter],
        };

        if (!state.active) {
            drain(state, demand);
            return waiter.promise;
        }
        if (demandCanJoin(state.active, demand)) {
            state.active.waiters.push(waiter);
            for (const recorder of demand.recorders) state.active.recorders.add(recorder);
            emit(state, state.active, "asset_refresh_join", {
                joined_reason: demand.reason,
            });
            return waiter.promise;
        }

        state.pending = mergeDemand(state.pending, demand);
        emit(state, state.pending, "asset_refresh_followup_queued", {
            active_request_id: state.active.requestId,
        });
        if (demand.manual && state.retryWait) {
            state.pending.waiters.push(...state.active.waiters);
            state.active.waiters.length = 0;
            state.retryWait.cancel();
        }
        return waiter.promise;
    };

    const markMutation = (projectId, reason = "asset_mutation") => {
        const normalized = normalizedProjectId(projectId);
        if (!normalized) return 0;
        const state = stateFor(normalized);
        state.mutationEpoch += 1;
        mutationEpochs.set(normalized, state.mutationEpoch);
        if (state.active) {
            const followup = {
                projectId: normalized,
                mode: "read",
                waveId: allocateAssetRefreshWave(`mutation-${state.mutationEpoch}`),
                requiredVersion: "",
                unknownVersion: true,
                epoch: state.mutationEpoch,
                policy: state.active.policy,
                policySignature: state.active.policySignature,
                manual: false,
                reason,
                requestId: "",
                recorders: new Set(state.active.recorders),
                waiters: [...state.active.waiters],
            };
            state.active.waiters.length = 0;
            state.pending = mergeDemand(state.pending, followup);
            emit(state, state.pending, "asset_refresh_followup_queued", {
                active_request_id: state.active.requestId,
                cause: "mutation_epoch",
            });
        } else {
            states.delete(normalized);
        }
        return state.mutationEpoch;
    };

    // ---- Detail lane -------------------------------------------------------------
    // Kept in its own map: list/sync `states` entries are deleted when that lane goes
    // idle, and a detail lane parked on a deleted state would run beside a second one.
    // It shares only the mutation epoch, which is the gate both lanes must honour.
    const detailStates = new Map();

    const detailStateFor = (projectId) => {
        let state = detailStates.get(projectId);
        if (!state) {
            state = { projectId, queue: new Map(), active: null, pumpScheduled: false };
            detailStates.set(projectId, state);
        }
        return state;
    };

    const liveEpoch = (projectId) => states.get(projectId)?.mutationEpoch || mutationEpochs.get(projectId) || 0;

    const detailKey = (kind, assetId) => `${kind}:${assetId}`;

    const settleDetailWaiter = (waiter, assetId, result) => {
        if (waiter.settled) return;
        waiter.settled = true;
        waiter.onSettle(assetId, result);
    };

    const nextDetailBatch = (state) => {
        let best = null;
        for (const entry of state.queue.values()) {
            if (!best || entry.rank < best.rank) best = entry;
        }
        if (!best) return null;
        // An entry isolated after a failed batch is read alone, so one unreadable asset
        // cannot fail the healthy ones it was batched with a second time.
        if (best.isolated) return [best];
        const batch = [];
        for (const entry of state.queue.values()) {
            if (entry.kind !== best.kind || entry.rank !== best.rank || entry.isolated) continue;
            batch.push(entry);
            if (batch.length >= ASSET_DETAIL_BATCH_LIMIT) break;
        }
        return batch;
    };

    const releaseIdleDetailState = (state) => {
        if (!state.active && !state.queue.size && !state.pumpScheduled
            && detailStates.get(state.projectId) === state) {
            detailStates.delete(state.projectId);
        }
    };

    // Batches form one microtask after a demand, so demands made in the same turn are
    // ranked together: a selection requested just after its own preload window still
    // dispatches first.
    const scheduleDetailPump = (state) => {
        if (state.pumpScheduled) return;
        state.pumpScheduled = true;
        queueMicrotask(() => {
            state.pumpScheduled = false;
            pumpDetails(state);
        });
    };

    const pumpDetails = (state) => {
        if (state.active) return;
        const batch = nextDetailBatch(state);
        if (!batch) {
            releaseIdleDetailState(state);
            return;
        }
        for (const entry of batch) state.queue.delete(detailKey(entry.kind, entry.assetId));
        const kind = batch[0].kind;
        const epoch = liveEpoch(state.projectId);
        const active = {
            kind,
            epoch,
            requestId: `asset-detail-${++requestSequence}`,
            entries: new Map(batch.map((entry) => [entry.assetId, entry])),
        };
        state.active = active;
        // Read live, so a demand that joins after dispatch still sees this read's outcome.
        const diag = {
            requestId: active.requestId,
            mode: `detail:${kind}`,
            epoch,
            get recorders() {
                return new Set([...active.entries.values()].flatMap((entry) => [...entry.recorders]));
            },
            reason: `detail:${DETAIL_PRIORITY_NAMES[batch[0].rank] || "background"}`,
        };
        const startedAt = performance.now();
        emit(state, diag, "asset_detail_request", { asset_count: batch.length });
        // The lane is released BEFORE any waiter hears the outcome: a consumer that asks
        // again from inside its callback must queue a fresh read, never join this finished
        // one, whose waiters will never be settled again.
        const release = () => {
            if (state.active === active) state.active = null;
        };
        let run;
        try {
            run = Promise.resolve(detailRequest({ projectId: state.projectId, kind, assetIds: [...active.entries.keys()] }));
        } catch (error) {
            run = Promise.reject(error);
        }
        run
            .then((response) => {
                release();
                const live = liveEpoch(state.projectId);
                emit(state, diag, "asset_detail_response", {
                    served_version: response.modifiedAt,
                    duration_ms: performance.now() - startedAt,
                    live_epoch: live,
                });
                if (live !== epoch) {
                    // An asset write landed while this read was in flight. Nothing from it
                    // may populate a gallery; re-read at the new epoch, boundedly.
                    const exhausted = [];
                    for (const entry of active.entries.values()) {
                        entry.requeues += 1;
                        if (entry.requeues > DETAIL_SUPERSEDE_REQUEUE_LIMIT) exhausted.push(entry);
                        else enqueueDetailEntry(state, entry);
                    }
                    emit(state, diag, "asset_detail_supersede", { live_epoch: live, exhausted_count: exhausted.length });
                    const error = new Error("Assets kept changing while loading details.");
                    error.code = "asset_detail_superseded";
                    for (const entry of exhausted) {
                        for (const waiter of [...entry.waiters]) {
                            settleDetailWaiter(waiter, entry.assetId, { status: "error", error, epoch });
                        }
                    }
                    return;
                }
                for (const entry of active.entries.values()) {
                    const listed = Object.prototype.hasOwnProperty.call(response.revisions, entry.assetId);
                    const result = listed
                        ? { status: "ok", revision: String(response.revisions[entry.assetId] || ""),
                            value: response.values[entry.assetId], modifiedAt: response.modifiedAt, epoch }
                        : { status: "missing", modifiedAt: response.modifiedAt, epoch };
                    for (const waiter of [...entry.waiters]) settleDetailWaiter(waiter, entry.assetId, result);
                }
            })
            .catch((error) => {
                release();
                emit(state, diag, "asset_detail_error", {
                    status: Number(error?.status) || 0,
                    error_code: String(error?.code || error?.name || "error"),
                    duration_ms: performance.now() - startedAt,
                });
                // A server error on a shared batch may belong to one asset (an unreadable
                // component fails the whole read): errors belong to their own asset, so each
                // id is read once more on its own before any of them is reported.
                if (Number(error?.status) >= 500 && active.entries.size > 1) {
                    for (const entry of active.entries.values()) {
                        entry.isolated = true;
                        enqueueDetailEntry(state, entry);
                    }
                    return;
                }
                for (const entry of active.entries.values()) {
                    for (const waiter of [...entry.waiters]) {
                        settleDetailWaiter(waiter, entry.assetId, { status: "error", error, epoch });
                    }
                }
            })
            .finally(() => {
                release();
                scheduleDetailPump(state);
            });
    };

    // Re-queue keeps the entry's waiters and highest priority; a same-key entry already
    // queued absorbs it rather than duplicating the read.
    const enqueueDetailEntry = (state, entry) => {
        const key = detailKey(entry.kind, entry.assetId);
        const existing = state.queue.get(key);
        if (!existing) {
            state.queue.set(key, entry);
            return;
        }
        existing.rank = Math.min(existing.rank, entry.rank);
        // The supersede budget is per asset read, not per entry object: absorbing must not
        // reset it.
        existing.requeues = Math.max(existing.requeues, entry.requeues);
        existing.isolated = existing.isolated || !!entry.isolated;
        existing.waiters.push(...entry.waiters);
        for (const recorder of entry.recorders) existing.recorders.add(recorder);
    };

    const requestDetails = (input = {}) => {
        const projectId = normalizedProjectId(input.projectId);
        const kind = input.kind === "search" ? "search" : "provenance";
        const assetIds = [...new Set((input.assetIds || []).map(String).filter(Boolean))];
        const results = new Map();
        if (!projectId || !assetIds.length) {
            return { promise: Promise.resolve(results), cancel() {} };
        }
        const state = detailStateFor(projectId);
        const rank = detailRank(input.priority);
        const owner = String(input.ownerId || "");
        const recorder = typeof input.diagnosticRecorder === "function" ? input.diagnosticRecorder : null;
        const done = deferred();
        let remaining = assetIds.length;
        const waiters = [];
        const onSettle = (assetId, result) => {
            results.set(assetId, result);
            try { input.onResult?.(assetId, result); } catch (error) {
                console.warn("[Sonder] Asset detail consumer failed:", error);
            }
            remaining -= 1;
            if (remaining === 0) done.resolve(results);
        };
        for (const assetId of assetIds) {
            const waiter = { owner, rank, settled: false, onSettle };
            waiters.push({ assetId, waiter });
            // Shared in-flight read: join it rather than queue a duplicate. It finishes
            // normally even if a newer selection outranks what is queued behind it. Only a
            // read of the same kind dispatched at the live mutation epoch can be joined; one
            // an asset write has already overtaken would only be superseded. No other
            // generation boundary is needed here: a detail is valid for exactly the revision
            // it reports, and every consumer checks that against the list it shows.
            const active = state.active;
            const inFlight = active?.kind === kind && active.epoch === liveEpoch(projectId)
                ? active.entries.get(assetId)
                : null;
            if (inFlight) {
                inFlight.waiters.push(waiter);
                // A joining selection raises the read's priority in case it must be redone.
                inFlight.rank = Math.min(inFlight.rank, rank);
                if (recorder) inFlight.recorders.add(recorder);
                continue;
            }
            enqueueDetailEntry(state, {
                kind,
                assetId,
                rank,
                requeues: 0,
                waiters: [waiter],
                recorders: new Set(recorder ? [recorder] : []),
            });
        }
        scheduleDetailPump(state);
        return {
            promise: done.promise,
            // Withdraws this call's not-yet-dispatched ids only; dispatched ones finish.
            cancel() {
                const own = new Set(waiters.map((item) => item.waiter));
                cancelQueuedWaiters(state, (waiter) => own.has(waiter));
            },
        };
    };

    const cancelQueuedWaiters = (state, predicate) => {
        const cancelled = { status: "cancelled" };
        // Iterate a snapshot: a consumer that asks again from inside its cancellation
        // callback queues new work this pass must not visit.
        for (const [key, entry] of [...state.queue.entries()]) {
            if (state.queue.get(key) !== entry) continue;
            const kept = [];
            for (const waiter of entry.waiters) {
                if (predicate(waiter, entry)) settleDetailWaiter(waiter, entry.assetId, cancelled);
                else kept.push(waiter);
            }
            entry.waiters = kept;
            if (!kept.length) state.queue.delete(key);
            else entry.rank = Math.min(...kept.map((waiter) => waiter.rank));
        }
        releaseIdleDetailState(state);
    };

    // New selections outrank unstarted preload work: an owner withdraws its queued waiters
    // at the given priorities before requesting the new window.
    const cancelDetails = ({ projectId, ownerId, priorities = null } = {}) => {
        const state = detailStates.get(normalizedProjectId(projectId));
        if (!state) return;
        const owner = String(ownerId || "");
        // Unknown names select nothing rather than falling back to background.
        const ranks = priorities
            ? new Set(priorities.filter((name) => DETAIL_PRIORITY_NAMES.includes(name)).map(detailRank))
            : null;
        cancelQueuedWaiters(state, (waiter) => waiter.owner === owner && (!ranks || ranks.has(waiter.rank)));
    };

    return {
        request: requestRefresh,
        requestDetails,
        cancelDetails,
        markMutation,
        getMutationEpoch(projectId) {
            const normalized = normalizedProjectId(projectId);
            return states.get(normalized)?.mutationEpoch || mutationEpochs.get(normalized) || 0;
        },
        _debugState(projectId) {
            return states.get(normalizedProjectId(projectId)) || null;
        },
        _debugDetailState(projectId) {
            return detailStates.get(normalizedProjectId(projectId)) || null;
        },
    };
}

export function allocateAssetRefreshWave(reason = "refresh") {
    waveSequence += 1;
    return `${String(reason || "refresh")}:${Date.now().toString(36)}:${waveSequence.toString(36)}`;
}

const pageCoordinator = createAssetRefreshCoordinator();

export function requestProjectAssetRefresh(options) {
    return pageCoordinator.request(options);
}

// Detail-only demand (provenance or tracked-metadata search projection). Resolves to a
// Map of asset id -> {status: ok|missing|error|cancelled, ...}; it never rejects and
// never satisfies, joins or replaces a list/sync request.
export function requestProjectAssetDetails(options) {
    return pageCoordinator.requestDetails(options);
}

export function cancelProjectAssetDetails(options) {
    return pageCoordinator.cancelDetails(options);
}

export function markProjectAssetMutation(projectId, reason) {
    return pageCoordinator.markMutation(projectId, reason);
}

export function getProjectAssetMutationEpoch(projectId) {
    return pageCoordinator.getMutationEpoch(projectId);
}
