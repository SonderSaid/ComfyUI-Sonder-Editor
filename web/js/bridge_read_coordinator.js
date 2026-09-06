// Page-scoped coordinator for the read-only Reference, Guide, and Driver projections.
// Resource identity is the complete request URL. Generation identity belongs
// to the caller's logical refresh wave; this module never infers it from time.

let generationSequence = 0;
let requestSequence = 0;

function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((onResolve, onReject) => {
        resolve = onResolve;
        reject = onReject;
    });
    return { promise, resolve, reject };
}

function diagnosticHeaderValue(value, fallback) {
    const normalized = String(value || fallback || "")
        .replace(/[^\x20-\x7e]/g, "?")
        .slice(0, 256);
    return normalized || String(fallback || "unknown");
}

async function defaultRequest({ url, generation, origin, requestId, label, nodeId }) {
    const response = await fetch(url, {
        headers: {
            [`X-Sonder-${label}-Node-Id`]: diagnosticHeaderValue(nodeId, "unknown"),
            [`X-Sonder-${label}-Request-Id`]: diagnosticHeaderValue(requestId, label.toLowerCase()),
            [`X-Sonder-${label}-Generation`]: diagnosticHeaderValue(generation, "unknown"),
            [`X-Sonder-${label}-Origin`]: diagnosticHeaderValue(origin, "refresh"),
        },
    });
    if (!response.ok) {
        const error = new Error(`${label} bridge fetch failed: ${response.status}`);
        error.status = Number(response.status) || 0;
        throw error;
    }
    return response.json();
}

function settle(waiters, payload, error = null) {
    for (const waiter of waiters) {
        if (error) waiter.reject(error);
        else waiter.resolve(payload);
    }
    waiters.length = 0;
}

function mergeTrailing(current, incoming) {
    if (!current) return incoming;
    incoming.waiters.unshift(...current.waiters);
    current.waiters.length = 0;
    return incoming;
}

export function createBridgeReadCoordinator({ request = defaultRequest, label = "Reference" } = {}) {
    const entries = new Map();

    const evictIfIdle = (url, entry) => {
        if (!entry.active && !entry.trailing && entries.get(url) === entry) {
            entries.delete(url);
        }
    };

    const drain = (url, entry, demand) => {
        entry.active = demand;
        requestSequence += 1;
        demand.requestId = `${label.toLowerCase()}-${Date.now().toString(36)}-${requestSequence.toString(36)}`;
        Promise.resolve().then(() => request({
            url,
            label,
            nodeId: demand.nodeId,
            generation: demand.generation,
            origin: demand.origin,
            requestId: demand.requestId,
        })).then((payload) => {
            if (entry.active !== demand) return;
            entry.active = null;
            settle(demand.waiters, payload);
            if (entry.trailing) {
                const trailing = entry.trailing;
                entry.trailing = null;
                drain(url, entry, trailing);
            } else {
                evictIfIdle(url, entry);
            }
        }).catch((error) => {
            if (entry.active !== demand) return;
            entry.active = null;
            settle(demand.waiters, null, error);
            if (entry.trailing) {
                const trailing = entry.trailing;
                entry.trailing = null;
                drain(url, entry, trailing);
            } else {
                evictIfIdle(url, entry);
            }
        });
    };

    const requestPayload = ({ url, generation, origin = "refresh", nodeId = "" } = {}) => {
        const resourceUrl = String(url || "");
        const refreshGeneration = String(generation || "");
        if (!resourceUrl) return Promise.reject(new Error(`${label} bridge URL is required`));
        if (!refreshGeneration) {
            return Promise.reject(new Error(`${label} refresh generation is required`));
        }

        const waiter = deferred();
        let entry = entries.get(resourceUrl);
        if (!entry) {
            entry = { active: null, trailing: null };
            entries.set(resourceUrl, entry);
        }

        if (!entry.active) {
            drain(resourceUrl, entry, {
                generation: refreshGeneration,
                origin: String(origin || "refresh"),
                nodeId: String(nodeId ?? ""),
                requestId: "",
                waiters: [waiter],
            });
            return waiter.promise;
        }
        if (entry.active.generation === refreshGeneration) {
            entry.active.waiters.push(waiter);
            return waiter.promise;
        }
        if (entry.trailing?.generation === refreshGeneration) {
            entry.trailing.waiters.push(waiter);
            return waiter.promise;
        }

        entry.trailing = mergeTrailing(entry.trailing, {
            generation: refreshGeneration,
            origin: String(origin || "refresh"),
            nodeId: String(nodeId ?? ""),
            requestId: "",
            waiters: [waiter],
        });
        return waiter.promise;
    };

    return {
        request: requestPayload,
        _debugSize() {
            return entries.size;
        },
        _debugEntry(url) {
            return entries.get(String(url || "")) || null;
        },
    };
}

export function allocateBridgeReadGeneration(origin = "refresh") {
    generationSequence += 1;
    const label = String(origin || "refresh").replace(/[^a-zA-Z0-9_-]+/g, "-").slice(0, 48);
    return `${label || "refresh"}:${Date.now().toString(36)}:${generationSequence.toString(36)}`;
}

export const allocateBridgeReferenceGeneration = allocateBridgeReadGeneration;
export const allocateBridgeGuideGeneration = allocateBridgeReadGeneration;
export const allocateBridgeDriverGeneration = allocateBridgeReadGeneration;

const referenceCoordinator = createBridgeReadCoordinator({ label: "Reference" });
const guideCoordinator = createBridgeReadCoordinator({ label: "Guide" });
const driverCoordinator = createBridgeReadCoordinator({ label: "Driver" });
export const requestBridgeReferencePayload = (options) => referenceCoordinator.request(options);
export const requestBridgeGuidePayload = (options) => guideCoordinator.request(options);
export const requestBridgeDriverPayload = (options) => driverCoordinator.request(options);

// One scheduling policy for all bridge families. Hosts retain their node
// lifecycles and projection; only ephemeral refresh waves live here.
export function createBridgeRefreshScheduler({ dispatch, getTargets = () => [] }) {
    const pendingWaves = new Map();
    return ({ origin = "refresh", modifiedAt = "", force = false,
        targets = null, delayMs = 0 } = {}) => {
        const normalizedOrigin = String(origin || "refresh");
        // Canonical-id and folder-alias version notifications name one wave.
        const stableGeneration = !force && modifiedAt ? `project-version:${String(modifiedAt)}` : "";
        const generation = force ? allocateBridgeReadGeneration(normalizedOrigin)
            : (stableGeneration || pendingWaves.get("automatic")?.generation
                || allocateBridgeReadGeneration(normalizedOrigin));
        const waveKey = force ? generation : (stableGeneration || "automatic");
        let wave = pendingWaves.get(waveKey);
        const created = !wave;
        if (!wave) {
            wave = { generation, origins: new Set(), targets: new Set(), all: false };
            pendingWaves.set(waveKey, wave);
        }
        wave.origins.add(normalizedOrigin);
        if (targets == null) {
            wave.all = true;
            wave.targets.clear();
        } else if (!wave.all) {
            for (const node of targets) if (node) wave.targets.add(node);
        }
        if (created) window.setTimeout(() => {
            if (pendingWaves.get(waveKey) !== wave) return;
            pendingWaves.delete(waveKey);
            const meta = { generation: wave.generation,
                origin: [...wave.origins].sort().join("+") || "refresh" };
            for (const node of wave.all ? getTargets() : wave.targets) dispatch(node, meta);
        }, Math.max(0, Number(delayMs) || 0));
        return wave.generation;
    };
}
