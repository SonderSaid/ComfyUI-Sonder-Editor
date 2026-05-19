const projectVersions = new Map();
let fetchPatchInstalled = false;

function normalizeProjectId(projectId) {
    return String(projectId || "").trim();
}

function methodIsMutating(method) {
    return !["GET", "HEAD", "OPTIONS"].includes(String(method || "GET").toUpperCase());
}

function projectIdFromUrl(url) {
    const match = String(url || "").match(/\/sonder-editor\/project\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : "";
}

export function rememberProjectVersion(projectId, modifiedAt) {
    const normalizedProjectId = normalizeProjectId(projectId);
    if (!normalizedProjectId || !modifiedAt) return;
    projectVersions.set(normalizedProjectId, String(modifiedAt));
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

export function getProjectVersion(projectId) {
    return projectVersions.get(normalizeProjectId(projectId)) || "";
}

export function installProjectVersionFetchPatch() {
    if (fetchPatchInstalled || typeof window === "undefined" || typeof window.fetch !== "function") return;
    fetchPatchInstalled = true;
    const nativeFetch = window.fetch.bind(window);

    window.fetch = async (input, init = {}) => {
        const requestUrl = typeof input === "string" ? input : input?.url;
        const method = String(init?.method || input?.method || "GET").toUpperCase();
        const projectId = projectIdFromUrl(requestUrl);
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

        const response = await nativeFetch(input, nextInit);
        if (projectId) {
            response.clone().json()
                .then((payload) => rememberProjectVersionFromPayload(payload, projectId))
                .catch(() => {});
        }
        return response;
    };
}
