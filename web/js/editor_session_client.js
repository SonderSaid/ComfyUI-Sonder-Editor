const { api } = window.comfyAPI.api;

function apiJson(path, body) {
    return fetch(api.apiURL(path), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
    }).then(async (resp) => {
        let payload = {};
        try {
            payload = await resp.json();
        } catch (_err) {
            payload = {};
        }
        return { ...payload, http_ok: resp.ok, status: resp.status };
    });
}

function apiGetJson(path) {
    return fetch(api.apiURL(path)).then(async (resp) => {
        let payload = {};
        try {
            payload = await resp.json();
        } catch (_err) {
            payload = {};
        }
        return { ...payload, http_ok: resp.ok, status: resp.status };
    });
}

export function newEditorSessionId(prefix = "editor") {
    const random = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return `${prefix}-${random}`;
}

function hostQuery(hostId = "", sourceNodeId = "") {
    const params = new URLSearchParams();
    if (hostId) params.set("host_id", hostId);
    if (sourceNodeId) params.set("source_node_id", sourceNodeId);
    const qs = params.toString();
    return qs ? `?${qs}` : "";
}

export function claimEditorSession(projectId, sessionId, hostMode, owner = {}, handoffToken = "", hostId = "") {
    return apiJson(`/sonder-editor/session/${encodeURIComponent(projectId)}/claim`, {
        ...owner,
        host_id: hostId || owner?.host_id || "",
        session_id: sessionId,
        host_mode: hostMode,
        handoff_token: handoffToken,
    });
}

export function heartbeatEditorSession(projectId, sessionId, hostId = "", sourceNodeId = "") {
    return apiJson(`/sonder-editor/session/${encodeURIComponent(projectId)}/heartbeat`, {
        host_id: hostId,
        source_node_id: sourceNodeId,
        session_id: sessionId,
    });
}

export function heartbeatCanvasHost(projectId, sessionId, hostId = "", sourceNodeId = "", owner = {}) {
    return apiJson(`/sonder-editor/session/${encodeURIComponent(projectId)}/canvas_host`, {
        ...owner,
        host_id: hostId || owner?.host_id || "",
        source_node_id: sourceNodeId || owner?.source_node_id || "",
        session_id: sessionId,
    });
}

export function getEditorSession(projectId, hostId = "", sourceNodeId = "") {
    return apiGetJson(`/sonder-editor/session/${encodeURIComponent(projectId)}${hostQuery(hostId, sourceNodeId)}`);
}

export function releaseEditorSession(projectId, sessionId, force = false, hostId = "", sourceNodeId = "") {
    return apiJson(`/sonder-editor/session/${encodeURIComponent(projectId)}/release`, {
        host_id: hostId,
        source_node_id: sourceNodeId,
        session_id: sessionId,
        force,
    });
}

export function createEditorHandoff(projectId, sessionId, hostId = "", sourceNodeId = "") {
    return apiJson(`/sonder-editor/session/${encodeURIComponent(projectId)}/handoff`, {
        host_id: hostId,
        source_node_id: sourceNodeId,
        session_id: sessionId,
    });
}

export function getEditorWidgetState(projectId, sourceNodeId = "", hostId = "") {
    const qs = hostQuery(hostId, sourceNodeId);
    return apiGetJson(`/sonder-editor/project/${encodeURIComponent(projectId)}/widget_state${qs}`);
}

export function putEditorWidgetState(projectId, sourceNodeId, sessionId, values = {}, { seed = false, hostId = "" } = {}) {
    return fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(projectId)}/widget_state`), {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
            host_id: hostId,
            source_node_id: sourceNodeId || "",
            session_id: sessionId || "",
            values,
            seed,
            replace: seed,
        }),
    }).then(async (resp) => {
        let payload = {};
        try {
            payload = await resp.json();
        } catch (_err) {
            payload = {};
        }
        return { ...payload, http_ok: resp.ok, status: resp.status };
    });
}
