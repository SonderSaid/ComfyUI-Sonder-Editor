const projectVersions = new Map();
let fetchPatchInstalled = false;

function normalizeProjectId(projectId) {
    return String(projectId || "").trim();
}

function methodIsMutating(method) {
    return !["GET", "HEAD", "OPTIONS"].includes(String(method || "GET").toUpperCase());
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
    // short form. A legitimate backward jump (none exists today) would need
    // an explicit map clear.
    const next = String(modifiedAt);
    const current = projectVersions.get(normalizedProjectId) || "";
    if (current && next < current) return;
    projectVersions.set(normalizedProjectId, next);
}

export function rememberProjectVersionFromPayload(payload, fallbackProjectId = "") {
    if (!payload || typeof payload !== "object") return;
    const project = payload.project && typeof payload.project === "object" ? payload.project : payload;
    const projectId = normalizeProjectId(project.project_id);
    const fallback = normalizeProjectId(fallbackProjectId);
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
    const projectId = normalizeProjectId(headerProjectId || fallbackProjectId);
    if (projectId && headerModifiedAt) {
        rememberProjectVersion(projectId, headerModifiedAt);
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
