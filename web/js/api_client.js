const projectVersions = new Map();
const projectAliases = new Map();
let fetchPatchInstalled = false;

const STALE_REPLAY_DELAYS_MS = [250, 1000, 4000];

function normalizeProjectId(projectId) {
    return String(projectId || "").trim();
}

function methodIsMutating(method) {
    return !["GET", "HEAD", "OPTIONS"].includes(String(method || "GET").toUpperCase());
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
}

export function projectIdFromUrl(url) {
    const match = String(url || "").match(/\/sonder-editor\/project\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : "";
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
}

export function resetProjectVersion(projectId, modifiedAt) {
    const normalizedProjectId = normalizeProjectId(projectId);
    if (!normalizedProjectId || !modifiedAt) return;
    const next = String(modifiedAt);
    const aliases = projectAliases.get(normalizedProjectId) || new Set([normalizedProjectId]);
    for (const alias of aliases) {
        projectVersions.set(alias, next);
    }
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
// carries `actual_modified_at` + the full project, so it can adopt the correct
// version and retry at once. Returns fetchProjectJson's `{ response, payload }`
// plus an `attempts` count so callers can emit reconcile diagnostics.
export async function postProjectJsonWithReconcile(
    url,
    init = {},
    { projectId = "", retryOnConflict = true, maxAttempts = 2 } = {},
) {
    let attempt = 0;
    while (true) {
        attempt += 1;
        const explicitIfMatch = new Headers(init?.headers || {}).get("If-Match") || "";
        const sentVersion = String(explicitIfMatch || getProjectVersion(projectId) || "")
            .replace(/^W\//, "")
            .replace(/^"|"$/g, "");
        try {
            const result = await fetchProjectJson(url, init, { projectId });
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
