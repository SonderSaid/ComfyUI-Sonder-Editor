// Data-only client for observed run outputs, one per editor host.
//
// Canvas, fullscreen and mounted hosts read their canvas host's owner scope with
// the session ids they already hold; the Sonder App reads its session's runs
// with its secret headers. No UI lives here: Output Views (Phase 3) consume it.
// `outputs_changed` project events carry no media, only which host or run
// recorded something; a host refetches on a hint for itself and polls otherwise.

const SDK_SECRET_HEADER = "X-Sonder-SDK-Session";
const SDK_TAB_HEADER = "X-Sonder-Tab-Session";
const DIAG_HINTS = 32;

// Every page that syncs a project feeds its `outputs_changed` events here
// (cross_tab_sync.js), whichever mode it is; clients pick their own.
const hintListeners = new Set();
const diag = { clients: new Map(), hints: [] };

export function publishOutputsChanged(projectId, event) {
    if (event?.type !== "outputs_changed") return;
    diag.hints.push({ at: Date.now(), project_id: String(projectId || ""), ...event });
    if (diag.hints.length > DIAG_HINTS) diag.hints.shift();
    for (const listener of [...hintListeners]) {
        try {
            listener(String(projectId || ""), event);
        } catch (err) {
            console.warn("[Sonder] outputs_changed listener failed:", err);
        }
    }
}

async function json(resp) {
    let payload = {};
    try { payload = await resp.json(); } catch (_error) { payload = {}; }
    return { ...payload, http_ok: resp.ok, status: resp.status };
}

// `identity()` returns {projectId, hostId, sessionId, workflowId?} for canvas/mounted hosts, or
// {projectId, sessionId, secret, tabSessionId, runIds} for the Sonder App.
export function createRunOutputsClient({ mode, identity, apiURL, fetchImpl = globalThis.fetch?.bind(globalThis), label = "" }) {
    const sdk = mode === "sdk";
    const base = () => {
        const id = identity() || {};
        return sdk
            ? `/sonder-editor/sdk/session/${encodeURIComponent(id.sessionId || "")}/runs`
            : `/sonder-editor/project/${encodeURIComponent(id.projectId || "")}/runs`;
    };
    const hostFields = () => {
        const id = identity() || {};
        return sdk ? {} : { host_id: id.hostId || "", session_id: id.sessionId || "" };
    };
    const headers = (extra = {}) => {
        const id = identity() || {};
        return sdk ? { ...extra, [SDK_SECRET_HEADER]: id.secret || "", [SDK_TAB_HEADER]: id.tabSessionId || "" } : extra;
    };
    const get = (path, params = {}) => {
        const query = new URLSearchParams({ ...hostFields(), ...params });
        return fetchImpl(apiURL(`${base()}${path}?${query}`), { headers: headers() }).then(json);
    };
    const post = (path, body) => fetchImpl(apiURL(`${base()}${path}`), {
        method: "POST",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify({ ...hostFields(), ...body }),
    }).then(json);

    // Whether an `outputs_changed` event concerns this host. The Sonder App
    // matches the run ids it knows; with none known yet, any hint counts and
    // the authorized read decides.
    const isOwnHint = (event) => {
        if (event?.type !== "outputs_changed") return false;
        const id = identity() || {};
        if (sdk) {
            const runIds = typeof id.runIds === "function" ? id.runIds() : id.runIds;
            if (!runIds || !runIds.length) return true;
            return Array.isArray(event.run_ids) && event.run_ids.some(runId => runIds.includes(runId));
        }
        return Array.isArray(event.host_ids) && event.host_ids.includes(id.hostId || "");
    };

    const client = {
        mode,
        // `workflowId` defaults to the host's own (canvas); empty reads every
        // workflow of the owner (mounted, Sonder App session).
        readOutputs(sources, { workflowId = (identity() || {}).workflowId || "" } = {}) {
            return get("/outputs", { sources: [...sources].join(","), workflow_id: workflowId });
        },
        readRun(promptId, { workflowId = (identity() || {}).workflowId || "" } = {}) {
            return get(`/${encodeURIComponent(promptId)}`, workflowId ? { workflow_id: workflowId } : {});
        },
        hold({ promptId, source, index }) {
            return post("/hold", { prompt_id: promptId, source, index });
        },
        release(holdId) {
            return post("/release", { hold_id: holdId });
        },
        isOwnHint,
        // `listener(event)` for each hint about this host's runs. Returns cleanup.
        onOutputsChanged(listener) {
            if (typeof listener !== "function") return () => {};
            const relay = (projectId, event) => {
                if (projectId !== String((identity() || {}).projectId || "")) return;
                if (isOwnHint(event)) listener(event);
            };
            hintListeners.add(relay);
            return () => hintListeners.delete(relay);
        },
        dispose() {
            diag.clients.delete(client);
        },
    };
    diag.clients.set(client, label || mode);
    installDiagnostics();
    return client;
}

function sessionDiagnosticsOn() {
    if (window.SONDER_DEBUG_SESSION === true) return true;
    try {
        return window.localStorage?.getItem?.("SONDER_DEBUG_SESSION") === "1";
    } catch (_error) {
        return false;
    }
}

// `window.__sonderRunOutputs` behind the session-diagnostic flag
// (`SONDER_DEBUG_SESSION`, or its localStorage form in tabs that never load the
// canvas controller): the live clients and the last hints, for probes.
function installDiagnostics() {
    if (typeof window === "undefined" || window.__sonderRunOutputs || !sessionDiagnosticsOn()) return;
    window.__sonderRunOutputs = {
        clients: () => [...diag.clients].map(([client, label]) => ({ label, client })),
        client: (label) => [...diag.clients].find(([, name]) => name === label)?.[0] || null,
        hints: () => diag.hints.slice(),
    };
}
