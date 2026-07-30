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

function defaultRequest(demand) {
    const api = window.comfyAPI.api.api;
    const query = demand.policySignature;
    const url = api.apiURL(
        `/sonder-editor/project/${encodeURIComponent(demand.projectId)}/assets${demand.mode === "sync" ? "/sync" : ""}?${query}`,
    );
    if (demand.mode === "sync") {
        return postProjectJsonWithReconcile(
            url,
            { method: "POST" },
            { projectId: demand.projectId },
        );
    }
    return fetchProjectJson(url, {}, { projectId: demand.projectId });
}

export function createAssetRefreshCoordinator({
    request = defaultRequest,
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

    return {
        request: requestRefresh,
        markMutation,
        getMutationEpoch(projectId) {
            const normalized = normalizedProjectId(projectId);
            return states.get(normalized)?.mutationEpoch || mutationEpochs.get(normalized) || 0;
        },
        _debugState(projectId) {
            return states.get(normalizedProjectId(projectId)) || null;
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

export function markProjectAssetMutation(projectId, reason) {
    return pageCoordinator.markMutation(projectId, reason);
}

export function getProjectAssetMutationEpoch(projectId) {
    return pageCoordinator.getMutationEpoch(projectId);
}
