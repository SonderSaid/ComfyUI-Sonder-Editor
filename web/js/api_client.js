const projectVersions = new Map();
const projectAliases = new Map();
const projectVersionListeners = new Set();
let fetchPatchInstalled = false;
let mutationRequestSequence = 0;
const mutationRequestNamespace = (() => {
    try {
        if (typeof globalThis.crypto?.randomUUID === "function") {
            return globalThis.crypto.randomUUID();
        }
    } catch (_) {}
    return `${Date.now().toString(36)}-${Math.random().toString(16).slice(2)}`;
})();

const STALE_REPLAY_DELAYS_MS = [250, 1000, 4000];

function normalizeProjectId(projectId) {
    return String(projectId || "").trim();
}

function methodIsMutating(method) {
    return !["GET", "HEAD", "OPTIONS"].includes(String(method || "GET").toUpperCase());
}

function nextMutationRequestId() {
    mutationRequestSequence += 1;
    return `mutation-${mutationRequestNamespace}-${mutationRequestSequence.toString(36)}`;
}

export function withMutationRequestDiagnostics(init = {}, attempt = 1) {
    const headers = new Headers(init?.headers || {});
    if (!headers.has("X-Sonder-Gesture-Id")) {
        headers.set("X-Sonder-Gesture-Id", "");
    }
    if (!headers.has("X-Sonder-Gesture-Kind")) {
        headers.set("X-Sonder-Gesture-Kind", "unscoped");
    }
    if (!headers.has("X-Sonder-Mutation-Coalesced-Count")) {
        headers.set("X-Sonder-Mutation-Coalesced-Count", "1");
    }
    headers.set("X-Sonder-Request-Id", nextMutationRequestId());
    headers.set("X-Sonder-Gesture-Attempt", String(Math.max(1, Number(attempt) || 1)));
    return { ...init, headers };
}

function associateProjectIds(firstProjectId, secondProjectId) {
    const first = normalizeProjectId(firstProjectId);
    const second = normalizeProjectId(secondProjectId);
    if (!first || !second || first === second) return;
    const aliases = new Set([
        first,
        second,
        ...(projectAliases.get(first) || []),
        ...(projectAliases.get(second) || []),
    ]);
    for (const projectId of aliases) {
        projectAliases.set(projectId, aliases);
    }
    shareCertificateStore(aliases);
}

/**
 * Whether two project ids name the same project: equal, or known aliases (the
 * canonical project UUID and the folder basename).
 */
export function sameProject(firstProjectId, secondProjectId) {
    const first = normalizeProjectId(firstProjectId);
    const second = normalizeProjectId(secondProjectId);
    if (!first || !second) return false;
    return first === second || !!projectAliases.get(first)?.has(second);
}

// ---------------------------------------------------------------------------
// Change certificates
//
// The server attaches a certificate to a committed scene edit saying which of
// two reads -- the live prompt compile and the Reference bridge read -- that one
// version transition can change (`server/change_certificates.py`). Consumers
// holding a result read at version A may keep it at version B only when an
// UNINTERRUPTED chain of certificates leads from A to B and none of them flags
// their read for their scene. Every doubt -- a missing, malformed, conflicting,
// evicted or discontinuous certificate -- answers "refresh". Certificates are
// ephemeral and page-local; nothing here is persisted.
// ---------------------------------------------------------------------------

const CERTIFICATE_SCHEMA = 1;
const CERTIFICATE_HEADER = "X-Sonder-Change-Certificate";
// Transitions retained per project. Eviction only ever costs a refresh.
const MAX_CERTIFIED_TRANSITIONS = 32;
const CONFLICTED = Symbol("conflicted certificate");
const certificateStores = new Map();

function newCertificateStore() {
    return { transitions: new Map() };
}

function certificateStoreFor(projectId) {
    const normalized = normalizeProjectId(projectId);
    if (!normalized) return null;
    let store = certificateStores.get(normalized);
    if (!store) {
        store = newCertificateStore();
        for (const alias of projectAliases.get(normalized) || [normalized]) {
            certificateStores.set(alias, store);
        }
        certificateStores.set(normalized, store);
    }
    return store;
}

function shareCertificateStore(aliases) {
    const distinct = new Set();
    for (const alias of aliases) {
        const store = certificateStores.get(alias);
        if (store) distinct.add(store);
    }
    // Two ids that each carried their own history are now one project. Their
    // chains were recorded against separate keys and cannot be proven
    // continuous with each other, so continuity restarts rather than merges.
    const populated = [...distinct].filter((store) => store.transitions.size > 0);
    const shared = populated.length > 1 ? newCertificateStore()
        : (populated[0] || [...distinct][0] || newCertificateStore());
    for (const alias of aliases) certificateStores.set(alias, shared);
}

function clearCertificates(projectId) {
    const normalized = normalizeProjectId(projectId);
    for (const alias of projectAliases.get(normalized) || [normalized]) {
        const store = certificateStores.get(alias);
        if (store) store.transitions.clear();
    }
}

function validCertificate(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    if (value.schema !== CERTIFICATE_SCHEMA) return false;
    for (const key of ["project_id", "scene_id", "base_modified_at", "modified_at"]) {
        if (typeof value[key] !== "string" || !value[key]) return false;
    }
    if (typeof value.prompt !== "boolean" || typeof value.bridge !== "boolean") return false;
    // Versions are ISO timestamps, so string order is time order; a
    // certificate must describe a transition forwards.
    return value.base_modified_at < value.modified_at;
}

/**
 * Record one server certificate. `reportedVersion` is the version the same
 * response or event reported; a certificate for any other result version is
 * rejected. Returns true when the certificate was accepted (or was already held).
 */
export function registerChangeCertificate(certificate, { projectId = "", reportedVersion = "" } = {}) {
    if (!validCertificate(certificate)) return false;
    if (reportedVersion && String(reportedVersion) !== certificate.modified_at) return false;
    associateProjectIds(projectId, certificate.project_id);
    const store = certificateStoreFor(projectId || certificate.project_id);
    if (!store) return false;
    const record = {
        scene_id: certificate.scene_id,
        modified_at: certificate.modified_at,
        prompt: certificate.prompt,
        bridge: certificate.bridge,
    };
    const existing = store.transitions.get(certificate.base_modified_at);
    if (existing === CONFLICTED) return false;
    if (existing) {
        const same = existing.scene_id === record.scene_id
            && existing.modified_at === record.modified_at
            && existing.prompt === record.prompt && existing.bridge === record.bridge;
        // The same transition described twice with different content means
        // neither can be trusted; the base stays poisoned until a reset.
        if (!same) store.transitions.set(certificate.base_modified_at, CONFLICTED);
        return same;
    }
    store.transitions.set(certificate.base_modified_at, record);
    while (store.transitions.size > MAX_CERTIFIED_TRANSITIONS) {
        store.transitions.delete(store.transitions.keys().next().value);
    }
    return true;
}

/** Register the certificate a `project_updated` websocket event carries, if any. */
export function registerChangeCertificateFromEvent(event) {
    if (!event || typeof event !== "object" || !event.change) return false;
    return registerChangeCertificate(event.change, {
        projectId: event.project_id || "",
        reportedVersion: event.modified_at || "",
    });
}

function registerChangeCertificateFromResponse(response, projectId, reportedVersion) {
    const raw = response?.headers?.get?.(CERTIFICATE_HEADER) || "";
    if (!raw) return;
    try {
        registerChangeCertificate(JSON.parse(raw), { projectId, reportedVersion });
    } catch (_error) {
        // A malformed certificate is no certificate: consumers refresh.
    }
}

/**
 * Whether a result read at `fromVersion` still answers at `toVersion` for
 * `dependency` ("prompt" | "bridge") on `sceneId`: true only when certificates
 * chain from one to the other without a gap and none flags that dependency on
 * that scene. A transition on another scene is scene-confined by construction.
 */
export function certifiedUnchanged({ projectId, sceneId, fromVersion, toVersion, dependency } = {}) {
    if (dependency !== "prompt" && dependency !== "bridge") return false;
    const from = String(fromVersion || "");
    const to = String(toVersion || "");
    const scene = String(sceneId || "");
    if (!from || !to || !scene) return false;
    if (from === to) return true;
    if (from > to) return false;
    const store = certificateStores.get(normalizeProjectId(projectId));
    if (!store) return false;
    let version = from;
    for (let step = 0; step <= MAX_CERTIFIED_TRANSITIONS; step += 1) {
        const transition = store.transitions.get(version);
        if (!transition || transition === CONFLICTED) return false;
        if (transition.scene_id === scene && transition[dependency] !== false) return false;
        version = transition.modified_at;
        if (version === to) return true;
        if (version > to) return false;
    }
    return false;
}

export function projectIdFromUrl(url) {
    const match = String(url || "").match(/\/sonder-editor\/project\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : "";
}

/**
 * Observe durable project-version movement. Every mutation response passes
 * through the version map, so this is the one page-level signal that catches
 * same-tab and cross-tab project writes alike — graph-side surfaces that cache
 * project-derived shape subscribe here instead of opening their own socket.
 * Returns an unsubscribe function. Listener errors never reach the caller.
 */
export function onProjectVersionChanged(callback) {
    if (typeof callback !== "function") return () => {};
    projectVersionListeners.add(callback);
    return () => projectVersionListeners.delete(callback);
}

function emitProjectVersionChanged(projectId, modifiedAt) {
    for (const listener of [...projectVersionListeners]) {
        try {
            listener(projectId, modifiedAt);
        } catch (error) {
            console.warn("[Sonder] project version listener failed:", error);
        }
    }
}

export function rememberProjectVersion(projectId, modifiedAt) {
    const normalizedProjectId = normalizeProjectId(projectId);
    if (!normalizedProjectId || !modifiedAt) return;
    // Monotonic (mutation-integrity F3): the fetch patch records versions from
    // EVERY response including GETs, so an out-of-order stale GET response must
    // never move the map backwards (it would regress If-Match headers and
    // defeat version-gated apply checks). modified_at is an ISO timestamp —
    // lexicographic compare is order-correct, incl. the zero-microsecond
    // short form. Legitimate backward jumps use resetProjectVersion through
    // the stale-response breaker or lower-actual 409 recovery path below.
    const next = String(modifiedAt);
    const current = projectVersions.get(normalizedProjectId) || "";
    if (current && next < current) return;
    projectVersions.set(normalizedProjectId, next);
    if (next !== current) emitProjectVersionChanged(normalizedProjectId, next);
}

export function resetProjectVersion(projectId, modifiedAt) {
    const normalizedProjectId = normalizeProjectId(projectId);
    if (!normalizedProjectId || !modifiedAt) return;
    const next = String(modifiedAt);
    const aliases = projectAliases.get(normalizedProjectId) || new Set([normalizedProjectId]);
    let changed = false;
    for (const alias of aliases) {
        changed = changed || (projectVersions.get(alias) || "") !== next;
        projectVersions.set(alias, next);
    }
    // A backward jump means the map was ahead of the server; no chain recorded
    // against those versions can be trusted to connect to what follows.
    clearCertificates(normalizedProjectId);
    if (changed) emitProjectVersionChanged(normalizedProjectId, next);
}

export function createStaleReplayGovernor() {
    let activeProjectId = "";
    let servedVersion = "";
    let consecutiveRejections = 0;

    const reset = (projectId = "") => {
        activeProjectId = normalizeProjectId(projectId);
        servedVersion = "";
        consecutiveRejections = 0;
    };

    return {
        reject(projectId, rawServedVersion) {
            const normalizedProjectId = normalizeProjectId(projectId);
            const nextServedVersion = String(rawServedVersion || "");
            if (normalizedProjectId !== activeProjectId) {
                reset(normalizedProjectId);
            }
            if (nextServedVersion === servedVersion) {
                consecutiveRejections += 1;
            } else {
                servedVersion = nextServedVersion;
                consecutiveRejections = 1;
            }
            if (consecutiveRejections > STALE_REPLAY_DELAYS_MS.length) {
                const rejectionCount = consecutiveRejections;
                reset(normalizedProjectId);
                return { action: "accept", rejectionCount };
            }
            return {
                action: "retry",
                delayMs: STALE_REPLAY_DELAYS_MS[consecutiveRejections - 1],
                rejectionCount: consecutiveRejections,
            };
        },
        reset,
    };
}

export function rememberProjectVersionFromPayload(payload, fallbackProjectId = "") {
    if (!payload || typeof payload !== "object") return;
    const project = payload.project && typeof payload.project === "object" ? payload.project : payload;
    const projectId = normalizeProjectId(project.project_id);
    const fallback = normalizeProjectId(fallbackProjectId);
    associateProjectIds(projectId, fallback);
    if (project.modified_at && projectId) {
        rememberProjectVersion(projectId, project.modified_at);
    }
    if (project.modified_at && fallback && fallback !== projectId) {
        rememberProjectVersion(fallback, project.modified_at);
    }
}

export function rememberProjectVersionFromResponse(response, fallbackProjectId = "") {
    if (!response?.headers) return;
    const headerProjectId = response.headers.get?.("X-Sonder-Project-Id") || "";
    const headerModifiedAt = response.headers.get?.("X-Sonder-Project-Modified-At") || "";
    const projectId = normalizeProjectId(headerProjectId);
    const fallback = normalizeProjectId(fallbackProjectId);
    associateProjectIds(projectId, fallback);
    // Before the version is remembered: remembering it notifies consumers, and
    // they must be able to see the certificate for the transition they are told of.
    registerChangeCertificateFromResponse(response, fallback || projectId, headerModifiedAt);
    if (headerModifiedAt) {
        if (projectId) rememberProjectVersion(projectId, headerModifiedAt);
        if (fallback && fallback !== projectId) rememberProjectVersion(fallback, headerModifiedAt);
    }
}

export function getProjectVersion(projectId) {
    return projectVersions.get(normalizeProjectId(projectId)) || "";
}

function withProjectVersionHeader(input, init = {}, fallbackProjectId = "") {
    const requestUrl = typeof input === "string" ? input : input?.url;
    const method = String(init?.method || input?.method || "GET").toUpperCase();
    const projectId = normalizeProjectId(fallbackProjectId || projectIdFromUrl(requestUrl));
    let nextInit = init || {};

    if (projectId && methodIsMutating(method)) {
        const version = getProjectVersion(projectId);
        if (version) {
            const headers = new Headers(input instanceof Request ? input.headers : undefined);
            new Headers(nextInit.headers || {}).forEach((value, key) => headers.set(key, value));
            if (!headers.has("If-Match")) {
                headers.set("If-Match", version);
                nextInit = { ...nextInit, headers };
            }
        }
    }

    return { init: nextInit, projectId };
}

async function parseResponsePayload(response) {
    const text = await response.text();
    if (!text) return null;
    try {
        return JSON.parse(text);
    } catch (_error) {
        return text;
    }
}

export async function fetchProjectJson(input, init = {}, { projectId: fallbackProjectId = "" } = {}) {
    const { init: nextInit, projectId } = withProjectVersionHeader(input, init, fallbackProjectId);
    const response = await fetch(input, nextInit);
    rememberProjectVersionFromResponse(response, projectId);
    const payload = await parseResponsePayload(response);
    if (payload && typeof payload === "object") {
        rememberProjectVersionFromPayload(payload, projectId);
    }

    if (!response.ok) {
        const message = payload && typeof payload === "object"
            ? (payload.error || payload.message || `Request failed: ${response.status}`)
            : (payload || `Request failed: ${response.status}`);
        const error = new Error(message);
        error.status = response.status;
        error.payload = payload;
        if (response.status === 409 && payload?.code === "project_version_conflict") {
            error.code = "project_version_conflict";
            error.expectedModifiedAt = payload.expected_modified_at || "";
            error.actualModifiedAt = payload.actual_modified_at || "";
            error.project = payload.project || null;
        }
        throw error;
    }

    return { response, payload };
}

// Versioned project write with immediate heal-and-retry from the 409 body.
// Unlike the scenes/queue governor (blind timed backoff for GETs that only learn
// staleness from a header), a versioned POST receives a 409 whose body already
// carries `actual_modified_at` plus the four-key project healing projection,
// so it can adopt the correct version and retry at once. Returns fetchProjectJson's `{ response, payload }`
// plus an `attempts` count so callers can emit reconcile diagnostics.
export async function postProjectJsonWithReconcile(
    url,
    init = {},
    { projectId = "", retryOnConflict = true, maxAttempts = 2 } = {},
) {
    let attempt = 0;
    while (true) {
        attempt += 1;
        // A retry is another physical request, not another user gesture. Keep
        // the gesture headers supplied by the enqueue site, but mint request
        // identity and attempt ordinal here for every actual send.
        const attemptInit = withMutationRequestDiagnostics(init, attempt);
        const explicitIfMatch = new Headers(attemptInit.headers || {}).get("If-Match") || "";
        const sentVersion = String(explicitIfMatch || getProjectVersion(projectId) || "")
            .replace(/^W\//, "")
            .replace(/^"|"$/g, "");
        try {
            const result = await fetchProjectJson(url, attemptInit, { projectId });
            return { ...result, attempts: attempt };
        } catch (error) {
            if (
                retryOnConflict
                && error?.code === "project_version_conflict"
                && attempt < maxAttempts
            ) {
                // fetchProjectJson already forward-adopted the 409's version
                // (monotonic). A *lower* actual means the client map is poisoned
                // ahead of the server, so force it back across both aliases. The
                // fetch patch restamps If-Match from the healed map on the retry.
                const actualVersion = String(error.actualModifiedAt || "");
                if (actualVersion && sentVersion && actualVersion < sentVersion) {
                    resetProjectVersion(projectId, actualVersion);
                }
                if (error.project) {
                    rememberProjectVersionFromPayload(error.project, projectId);
                }
                continue;
            }
            throw error;
        }
    }
}

export function installProjectVersionFetchPatch() {
    if (fetchPatchInstalled || typeof window === "undefined" || typeof window.fetch !== "function") return;
    fetchPatchInstalled = true;
    const nativeFetch = window.fetch.bind(window);

    window.fetch = async (input, init = {}) => {
        const requestUrl = typeof input === "string" ? input : input?.url;
        const method = String(init?.method || input?.method || "GET").toUpperCase();
        const { init: nextInit, projectId } = withProjectVersionHeader(input, init);

        const response = await nativeFetch(input, nextInit);
        if (projectId) {
            rememberProjectVersionFromResponse(response, projectId);
            response.clone().json()
                .then((payload) => rememberProjectVersionFromPayload(payload, projectId))
                .catch(() => {});
        }
        return response;
    };
}
