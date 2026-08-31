// Page-scoped coordinator for the read-only Reference shape projection.
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

async function defaultRequest({ url, generation, origin, requestId }) {
    const response = await fetch(url, {
        headers: {
            "X-Sonder-Reference-Request-Id": diagnosticHeaderValue(requestId, "reference"),
            "X-Sonder-Reference-Generation": diagnosticHeaderValue(generation, "unknown"),
            "X-Sonder-Reference-Origin": diagnosticHeaderValue(origin, "refresh"),
        },
    });
    if (!response.ok) {
        const error = new Error(`Reference bridge fetch failed: ${response.status}`);
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

export function createBridgeReferenceCoordinator({ request = defaultRequest } = {}) {
    const entries = new Map();

    const evictIfIdle = (url, entry) => {
        if (!entry.active && !entry.trailing && entries.get(url) === entry) {
            entries.delete(url);
        }
    };

    const drain = (url, entry, demand) => {
        entry.active = demand;
        requestSequence += 1;
        demand.requestId = `reference-${Date.now().toString(36)}-${requestSequence.toString(36)}`;
        Promise.resolve().then(() => request({
            url,
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

    const requestPayload = ({ url, generation, origin = "refresh" } = {}) => {
        const resourceUrl = String(url || "");
        const refreshGeneration = String(generation || "");
        if (!resourceUrl) return Promise.reject(new Error("Reference bridge URL is required"));
        if (!refreshGeneration) {
            return Promise.reject(new Error("Reference refresh generation is required"));
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

export function allocateBridgeReferenceGeneration(origin = "refresh") {
    generationSequence += 1;
    const label = String(origin || "refresh").replace(/[^a-zA-Z0-9_-]+/g, "-").slice(0, 48);
    return `${label || "refresh"}:${Date.now().toString(36)}:${generationSequence.toString(36)}`;
}

const pageCoordinator = createBridgeReferenceCoordinator();

export function requestBridgeReferencePayload(options) {
    return pageCoordinator.request(options);
}
