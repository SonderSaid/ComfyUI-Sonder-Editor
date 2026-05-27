import { installComfyApiShim } from "./comfy_api_shim.js";

installComfyApiShim();

const { api } = window.comfyAPI.api;

// Session diagnostic mode is gated by `window.SONDER_DEBUG_SESSION === true`.
// Persistent enable across reloads: set `localStorage.SONDER_DEBUG_SESSION = "1"`
// once; this bootstrap copies it into the window global on import.
const SESSION_DIAG_RING_MAX = 2048;
const _tabDiagEvents = [];
let _tabDiagBoot = null;
let _tabDiagSessionId = "";

if (typeof window !== "undefined" && !window.SONDER_DEBUG_SESSION) {
    try {
        if (window.localStorage?.getItem?.("SONDER_DEBUG_SESSION") === "1") {
            window.SONDER_DEBUG_SESSION = true;
        }
    } catch (_) {}
}

function isSessionDiagEnabled() {
    return typeof window !== "undefined" && window.SONDER_DEBUG_SESSION === true;
}
function tabDiagBootIfNeeded(sessionIdValue) {
    if (sessionIdValue) _tabDiagSessionId = sessionIdValue;
    if (!isSessionDiagEnabled() || _tabDiagBoot) return;
    _tabDiagBoot = {
        kind: "boot",
        t_wall: Date.now(),
        t_mono: performance.now(),
        build_marker: `tab:${_tabDiagSessionId || ""}`,
        href: typeof location !== "undefined" ? String(location.href || "") : "",
    };
    _tabDiagEvents.push({ ..._tabDiagBoot });
}
function tabDiagRecord(kind, payload) {
    if (!isSessionDiagEnabled()) return;
    tabDiagBootIfNeeded();
    if (_tabDiagEvents.length >= SESSION_DIAG_RING_MAX) _tabDiagEvents.shift();
    _tabDiagEvents.push({
        t_wall: Date.now(),
        t_mono: performance.now(),
        kind,
        ...(payload || {}),
    });
}

const params = new URLSearchParams(window.location.search);
const projectId = decodeURIComponent(window.location.pathname.split("/").pop() || "");
const handoffToken = params.get("handoff") || "";
const hostId = params.get("host_id") || "";
const sourceNodeId = params.get("source_node_id") || "";
const sessionWindowName = params.get("session_name") || "";
const statusEl = document.getElementById("status");
const sessionId = `tab-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
const STATUS_PILL_TOOLBAR_GAP = 12;
const STATUS_PILL_FALLBACK_RIGHT = 140;

const DEFAULT_WIDGET_VALUES = {
    scene_id: "",
    selection_start: 0,
    selection_end: 0,
    pre_context_frames: 0,
    post_context_frames: 0,
    mask_pre_offset: 0,
    mask_post_offset: 0,
    render_queue_active: true,
    take_placement_mode: "trimmed",
};

function setStatus(message) {
    if (statusEl) statusEl.textContent = message;
}

function makeStatusPill() {
    const pill = document.createElement("div");
    pill.style.cssText = `
        position: fixed;
        right: 12px;
        top: 10px;
        z-index: 10020;
        padding: 4px 8px;
        border-radius: 6px;
        background: rgba(21, 28, 36, 0.92);
        border: 1px solid rgba(88, 112, 137, 0.75);
        color: #90a0af;
        font: 11px "Segoe UI", Arial, sans-serif;
        pointer-events: none;
    `;
    pill.textContent = "Connecting";
    document.body.appendChild(pill);
    return pill;
}

function attachStatusPillOffset(pill) {
    let raf = 0;
    let observer = null;
    let observed = null;

    const applyFallback = () => {
        pill.style.right = `${STATUS_PILL_FALLBACK_RIGHT}px`;
    };

    const measure = () => {
        const toolbarButtons = document.querySelector("[data-fs-toolbar-buttons]");
        if (!toolbarButtons) {
            applyFallback();
            return false;
        }
        if (toolbarButtons !== observed && typeof ResizeObserver !== "undefined") {
            observer?.disconnect();
            observer = new ResizeObserver(() => schedule(false));
            observer.observe(toolbarButtons);
            observed = toolbarButtons;
        }
        const width = Math.ceil(toolbarButtons.getBoundingClientRect().width || toolbarButtons.offsetWidth || 0);
        if (width <= 0) {
            applyFallback();
            return false;
        }
        pill.style.right = `${width + STATUS_PILL_TOOLBAR_GAP}px`;
        return true;
    };

    const schedule = (retry = false) => {
        if (raf) cancelAnimationFrame(raf);
        raf = requestAnimationFrame(() => {
            raf = 0;
            if (!measure() && retry) requestAnimationFrame(measure);
        });
    };

    schedule(true);
    return {
        refresh: () => schedule(false),
        cleanup: () => {
            if (raf) cancelAnimationFrame(raf);
            observer?.disconnect();
        },
    };
}

function projectWidgetNode() {
    return {
        size: [1200, 800],
        setSize() {},
        computeSize() {
            return this.size;
        },
        setDirtyCanvas() {},
    };
}

async function fetchJson(path, init) {
    const resp = await fetch(api.apiURL(path), init);
    const payload = await resp.json().catch(() => ({}));
    return { ok: resp.ok, status: resp.status, payload };
}

class TabWidgetHost {
    constructor(projectId, hostId, sourceNodeId, sessionId, initialValues = {}) {
        this.projectId = projectId;
        this.hostId = hostId;
        this.sourceNodeId = sourceNodeId;
        this.sessionId = sessionId;
        this.values = { ...DEFAULT_WIDGET_VALUES, ...(initialValues || {}) };
        this.canvasHostConnected = false;
        this.sessionStatus = "";
        this.publishEnabled = false;
        this.publishRequested = false;
        this.lastPillSignature = "";
        this.onPublishRejected = null;
    }

    getValue(name, defaultValue = 0) {
        return Object.prototype.hasOwnProperty.call(this.values, name) ? this.values[name] : defaultValue;
    }

    setValue(name, value) {
        if (Object.is(this.values[name], value)) return;
        this.values[name] = value;
        if (!this.publishEnabled || !this.canvasHostConnected) return;
        this._publish({ [name]: value });
    }

    setValueLocal(name, value) {
        if (Object.is(this.values[name], value)) return;
        this.values[name] = value;
    }

    getNodeId() {
        return this.hostId || this.sourceNodeId || "tab";
    }

    getSize() {
        return [1200, 800];
    }

    setSize() {}

    computeSize() {
        return [1200, 800];
    }

    markDirty() {}

    apply(values = {}) {
        Object.assign(this.values, values || {});
    }

    setPublishingEnabled(enabled) {
        this.publishRequested = !!enabled;
        this._syncPublishing();
    }

    setCanvasConnected(connected) {
        this.canvasHostConnected = !!connected;
        this._syncPublishing();
    }

    setSessionStatus(status) {
        this.sessionStatus = status || "";
        this._syncPublishing();
    }

    _syncPublishing() {
        this.publishEnabled = !!this.publishRequested && this.canvasHostConnected && this.sessionStatus === "active";
    }

    _publish(values) {
        fetch(api.apiURL(`/sonder-editor/project/${encodeURIComponent(this.projectId)}/widget_state`), {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                host_id: this.hostId,
                source_node_id: this.sourceNodeId,
                session_id: this.sessionId,
                values,
            }),
        }).then(async (resp) => {
            if (resp.ok) return;
            let payload = {};
            try {
                payload = await resp.json();
            } catch (_err) {}
            this.onPublishRejected?.(payload?.code || `http_${resp.status}`);
        }).catch((err) => {
            console.warn("[Sonder] Failed to publish tab widget state:", err);
        });
    }
}

async function main() {
    if (!projectId) {
        setStatus("Missing project id.");
        return;
    }
    if (!handoffToken) {
        setStatus("Open the persistent tab from the fullscreen editor.");
        return;
    }
    if (!hostId) {
        setStatus("Missing editor host id. Reopen the tab from the fullscreen editor.");
        return;
    }

    tabDiagBootIfNeeded(sessionId);
    tabDiagRecord("tab_main_start", { project_id: projectId, host_id: hostId, source_node_id: sourceNodeId });

    const [
        { connectProjectSync },
        { EditorWidget },
        { installProjectVersionFetchPatch, rememberProjectVersionFromPayload, getProjectVersion },
        { claimEditorSession, heartbeatEditorSession, releaseEditorSession, getEditorWidgetState },
        { register: registerKeyboardConsumer, PRIORITY: KEYBOARD_PRIORITY },
    ] = await Promise.all([
        import("./cross_tab_sync.js"),
        import("./editor_widget.js"),
        import("./api_client.js"),
        import("./editor_session_client.js"),
        import("./keyboard_ownership.js"),
    ]);

    installProjectVersionFetchPatch();

    const projectResp = await fetchJson(`/sonder-editor/project/${encodeURIComponent(projectId)}`);
    if (!projectResp.ok) {
        setStatus(`Project load failed (${projectResp.status}).`);
        return;
    }
    rememberProjectVersionFromPayload(projectResp.payload, projectId);

    const initialState = await getEditorWidgetState(projectId, sourceNodeId, hostId);
    const host = new TabWidgetHost(projectId, hostId, sourceNodeId, sessionId, initialState.values || initialState.state || {});
    host.setCanvasConnected(!!initialState.canvas_host_connected);

    const claim = await claimEditorSession(projectId, sessionId, "tab", {
        host_id: hostId,
        source_node_id: sourceNodeId,
        browser_instance_id: sessionWindowName || sessionId,
        workflow_label: "Persistent editor tab",
    }, handoffToken, hostId);
    if (!claim.ok) {
        setStatus("This project is already open in another editor host.");
        return;
    }
    host.setSessionStatus(claim.owner?.status || "active");
    host.setPublishingEnabled(true);

    const node = projectWidgetNode();
    const editor = new EditorWidget(node, {
        host,
        hostMode: "tab",
        onFullscreenExit: () => window.close(),
    });
    editor.updateProject(projectId);
    editor.applyWidgetState(host.values);
    editor._enterFullscreen();
    document.body.classList.add("ready");
    if (sessionWindowName) {
        try {
            window.name = sessionWindowName;
        } catch (_err) {}
    }

    const statusPill = makeStatusPill();
    const statusPillOffset = attachStatusPillOffset(statusPill);
    const blocker = document.createElement("div");
    blocker.style.cssText = `
        position: fixed;
        inset: 0;
        z-index: 10010;
        display: none;
        align-items: center;
        justify-content: center;
        flex-direction: column;
        gap: 8px;
        background: rgba(15, 20, 26, 0.72);
        color: #f0c48b;
        font: 13px "Segoe UI", Arial, sans-serif;
        pointer-events: auto;
    `;
    const blockerMessage = document.createElement("div");
    blockerMessage.textContent = "Canvas not connected";
    const blockerDebug = document.createElement("div");
    blockerDebug.style.cssText = `
        max-width: min(720px, 80vw);
        color: #90a0af;
        font-size: 11px;
        line-height: 1.4;
        text-align: center;
        word-break: break-word;
    `;
    blocker.append(blockerMessage, blockerDebug);
    document.body.appendChild(blocker);

    const tabDebugEnabled = window.SONDER_DEBUG_TAB === true;
    blockerDebug.style.display = tabDebugEnabled ? "" : "none";

    const updateBlocker = () => {
        if (!host.canvasHostConnected) {
            blockerMessage.textContent = "Canvas not connected";
        } else if (host.sessionStatus === "orphaned") {
            blockerMessage.textContent = "Reconnecting session";
        } else if (host.sessionStatus === "released") {
            blockerMessage.textContent = "Session released";
        } else if (host.sessionStatus === "reconnecting") {
            blockerMessage.textContent = "Reconnecting session";
        } else if (host.sessionStatus !== "active") {
            blockerMessage.textContent = "Session not active";
        }
        blocker.style.display = host.canvasHostConnected && host.sessionStatus === "active" ? "none" : "flex";
    };

    const setCanvasConnected = (connected) => {
        const next = !!connected;
        const changed = host.canvasHostConnected !== next;
        if (!changed) return false;
        host.setCanvasConnected(next);
        updateBlocker();
        return changed;
    };

    const setSessionStatus = (status) => {
        const next = status || "";
        const changed = host.sessionStatus !== next;
        host.setSessionStatus(next);
        if (next === "active") {
            document.body.classList.add("ready");
        } else {
            document.body.classList.remove("ready");
        }
        updateBlocker();
        return changed;
    };

    host.onPublishRejected = (code) => {
        if (code === "canvas_host_disconnected") {
            setCanvasConnected(false);
            updatePill();
        } else if (code === "session_orphaned") {
            setSessionStatus("orphaned");
            updatePill();
        } else if (code === "no_owner" || code === "locked") {
            setStatus("Editor tab session was released.");
            setSessionStatus("released");
            updatePill();
        }
    };
    let transportState = "open";
    const updatePill = (state = "") => {
        if (state) transportState = state;
        let label = "Canvas connected";
        let color = "#bfe5c8";
        if (transportState === "reconnecting" || transportState === "closed") {
            label = "Reconnecting";
            color = "#f0c48b";
        } else if (!host.canvasHostConnected) {
            label = "Canvas not connected";
            color = "#f0c48b";
        } else if (host.sessionStatus === "orphaned") {
            label = "Reconnecting session";
            color = "#f0c48b";
        } else if (host.sessionStatus === "released") {
            label = "Session released";
            color = "#ff9f9f";
        } else if (host.sessionStatus === "reconnecting") {
            label = "Reconnecting session";
            color = "#f0c48b";
        } else if (host.sessionStatus !== "active") {
            label = "Session not active";
            color = "#f0c48b";
        }
        const signature = `${host.canvasHostConnected ? "1" : "0"}|${host.sessionStatus || ""}|${transportState}|${label}`;
        const changed = signature !== host.lastPillSignature;
        tabDiagRecord("pill_update", {
            canvas_host_connected: host.canvasHostConnected,
            session_status: host.sessionStatus,
            transport_state: transportState,
            label,
            signature,
            changed,
        });
        if (!changed) return;
        host.lastPillSignature = signature;
        statusPill.textContent = label;
        statusPill.style.color = color;
        statusPillOffset.refresh();
        updateBlocker();
    };
    setCanvasConnected(host.canvasHostConnected);
    updatePill();

    let lastPresenceDebug = "";
    const updatePresenceDebug = (payload = null) => {
        if (!tabDebugEnabled) return;
        if (host.canvasHostConnected) {
            blockerDebug.textContent = "";
            return;
        }
        if (!payload) {
            blockerDebug.textContent = `Waiting for host ${hostId} / node ${sourceNodeId || "unknown"}`;
            return;
        }
        const hosts = Array.isArray(payload.hosts) ? payload.hosts : [];
        const owners = Array.isArray(payload.owners) ? payload.owners : [];
        const hostList = hosts.map((item) => `${item.host_id || "?"} (${item.source_node_id || "?"})`).join(", ") || "none";
        const ownerList = owners.map((item) => `${item.host_mode || "?"}:${item.host_id || "?"}`).join(", ") || "none";
        blockerDebug.textContent = `Requested ${payload.requested?.host_id || hostId} / ${payload.requested?.source_node_id || sourceNodeId || "unknown"}; server hosts: ${hostList}; owners: ${ownerList}`;
    };
    updatePresenceDebug();

    const refreshCanvasPresence = () => {
        const sentAt = performance.now();
        tabDiagRecord("refresh_presence_send");
        getEditorWidgetState(projectId, sourceNodeId, hostId).then((state) => {
            tabDiagRecord("refresh_presence_recv", {
                duration_ms: performance.now() - sentAt,
                canvas_host_connected: !!state.canvas_host_connected,
                host_id_match: String(state.host_id || hostId) === hostId,
                source_match: !state.source_node_id || String(state.source_node_id) === sourceNodeId,
            });
            if (String(state.host_id || hostId) !== hostId) return;
            if (state.source_node_id && String(state.source_node_id) !== sourceNodeId) return;
            const changed = setCanvasConnected(!!state.canvas_host_connected);
            if (changed) updatePill();
            if (state.canvas_host_connected || !tabDebugEnabled) return;
            return fetchJson(`/sonder-editor/session/${encodeURIComponent(projectId)}/debug?host_id=${encodeURIComponent(hostId)}&source_node_id=${encodeURIComponent(sourceNodeId)}`)
                .then(({ ok, payload }) => {
                    if (!ok) return;
                    const signature = JSON.stringify(payload);
                    if (signature !== lastPresenceDebug) {
                        lastPresenceDebug = signature;
                        console.info("[Sonder] mounted tab host diagnostics", payload);
                    }
                    updatePresenceDebug(payload);
                });
        }).catch((err) => {
            tabDiagRecord("refresh_presence_error", {
                duration_ms: performance.now() - sentAt,
                error: String(err && err.message ? err.message : err),
            });
        });
    };
    const hostPresencePoll = setInterval(() => {
        refreshCanvasPresence();
    }, 2000);
    refreshCanvasPresence();

    let lastHeartbeatAt = 0;
    const sendSessionHeartbeat = () => {
        const sentAt = performance.now();
        const gapMs = lastHeartbeatAt > 0 ? sentAt - lastHeartbeatAt : 0;
        lastHeartbeatAt = sentAt;
        tabDiagRecord("session_heartbeat_send", { gap_ms: gapMs });
        heartbeatEditorSession(projectId, sessionId, hostId, sourceNodeId).then((result) => {
            tabDiagRecord("session_heartbeat_recv", {
                duration_ms: performance.now() - sentAt,
                ok: !!result.ok,
                code: result.code || "",
                owner_status: result.owner?.status || "",
                owner_session_id: result.owner?.session_id || "",
                canvas_host_connected: !!result.canvas_host_connected,
                matches_session: result.owner?.session_id === sessionId,
            });
            if (result.ok && result.owner?.session_id === sessionId) {
                setSessionStatus(result.owner.status || "active");
                setStatus("");
                updatePill("open");
            } else {
                setStatus("Editor tab session is no longer active.");
                setSessionStatus("released");
                updatePill();
            }
        }).catch((err) => {
            tabDiagRecord("session_heartbeat_error", {
                duration_ms: performance.now() - sentAt,
                error: String(err && err.message ? err.message : err),
            });
            setSessionStatus("reconnecting");
            updatePill("reconnecting");
        });
    };
    sendSessionHeartbeat();
    const heartbeat = setInterval(sendSessionHeartbeat, 10000);

    document.addEventListener("visibilitychange", () => {
        tabDiagRecord("visibilitychange", { state: document.visibilityState });
    });

    const sync = connectProjectSync(projectId, {
        onProjectUpdated: (event) => {
            const currentVersion = getProjectVersion(projectId);
            const shouldRefresh = !!(event.modified_at && event.modified_at !== currentVersion);
            tabDiagRecord("project_updated_recv", {
                event_modified_at: event.modified_at || "",
                current_version: currentVersion || "",
                refresh: shouldRefresh,
            });
            if (shouldRefresh) {
                editor.refresh(["project", "assets", "scenes", "queue"]);
            }
        },
        onWidgetStateChanged: (event) => {
            if (String(event.host_id || "") !== hostId) return;
            if (String(event.source_node_id || "") !== sourceNodeId) return;
            if (String(event.session_id || "") === sessionId) return;
            const values = event.values && typeof event.values === "object" ? event.values : {};
            host.apply(values);
            editor.applyWidgetState(values);
        },
        onHostPresenceChanged: (event) => {
            if (String(event.host_id || "") !== hostId) return;
            if (String(event.source_node_id || "") !== sourceNodeId) return;
            if (setCanvasConnected(!!event.canvas_host_connected)) updatePill();
        },
        onSubscribed: (event) => {
            if (event.host_id && String(event.host_id) !== hostId) return;
            if (event.source_node_id && String(event.source_node_id) !== sourceNodeId) return;
            if (Object.prototype.hasOwnProperty.call(event, "canvas_host_connected")) {
                if (setCanvasConnected(!!event.canvas_host_connected)) updatePill();
            } else {
                refreshCanvasPresence();
            }
        },
        onSessionChanged: (event) => {
            if (String(event.host_id || "") !== hostId) return;
            const owner = event.owner || null;
            if (owner?.session_id === sessionId) {
                setSessionStatus(owner.status || "active");
                if ((owner.status || "active") === "active") {
                    setStatus("");
                }
                updatePill();
            } else {
                setStatus("Editor tab session was released.");
                setSessionStatus("released");
                updatePill();
            }
        },
        onConnectionState: (state) => {
            tabDiagRecord("transport_state", { state });
            updatePill(state);
        },
    }, {
        clientId: hostId,
        hostId,
        sourceNodeId,
        sessionId,
        workflowLabel: "Persistent editor tab",
    });

    // Diagnostic-mode dump hotkey (Ctrl+Alt+Shift+D). Registered unconditionally;
    // the handler checks the flag at press time so it works even if the user
    // flips `window.SONDER_DEBUG_SESSION` on after page load.
    let diagHotkeyUnregister = null;
    try {
        diagHotkeyUnregister = registerKeyboardConsumer({
            id: `session-diag-dump-tab:${sessionId}`,
            priority: KEYBOARD_PRIORITY?.EDITOR ?? 10,
            keydown: (event) => {
                if (!isSessionDiagEnabled()) return false;
                if (!event.ctrlKey || !event.altKey || !event.shiftKey) return false;
                if (String(event.key || "").toLowerCase() !== "d") return false;
                dumpSessionDiagnostics().catch(() => {});
                return true;
            },
        });
    } catch (_) {
        diagHotkeyUnregister = null;
    }
    async function dumpSessionDiagnostics() {
        let backendDiag = null;
        try {
            const response = await fetch(api.apiURL(`/sonder-editor/session/${encodeURIComponent(projectId)}/diag`));
            if (response.ok) backendDiag = await response.json();
        } catch (_) { backendDiag = null; }
        const canvasDiag = (typeof window !== "undefined" && window.__SONDER_CANVAS_DIAG) || null;
        const bundle = {
            captured_at_wall: Date.now(),
            captured_at_mono: performance.now(),
            actor: "mounted_tab",
            session_id: sessionId,
            project_id: projectId,
            host_id: hostId,
            source_node_id: sourceNodeId,
            tab: {
                boot: _tabDiagBoot ? { ..._tabDiagBoot } : null,
                events: _tabDiagEvents.slice(),
            },
            canvas_page: canvasDiag ? {
                boot: canvasDiag.boot ? { ...canvasDiag.boot } : null,
                events: Array.isArray(canvasDiag.events) ? canvasDiag.events.slice() : [],
            } : null,
            backend: backendDiag,
        };
        const json = JSON.stringify(bundle, null, 2);
        try {
            await navigator.clipboard.writeText(json);
            console.info("[Sonder Session Diag] Copied tab diagnostic bundle to clipboard:", bundle);
        } catch (_) {
            try {
                const blob = new Blob([json], { type: "application/json" });
                const url = URL.createObjectURL(blob);
                const anchor = document.createElement("a");
                anchor.href = url;
                anchor.download = `sonder-session-diag-tab-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
                document.body.appendChild(anchor);
                anchor.click();
                document.body.removeChild(anchor);
                setTimeout(() => URL.revokeObjectURL(url), 5000);
                console.info("[Sonder Session Diag] Downloaded tab diagnostic bundle:", bundle);
            } catch (err) {
                console.warn("[Sonder Session Diag] Failed to copy/download tab diagnostic bundle:", err);
            }
        }
    }

    window.addEventListener("pagehide", () => {
        if (diagHotkeyUnregister) {
            try { diagHotkeyUnregister(); } catch (_) {}
            diagHotkeyUnregister = null;
        }
        clearInterval(heartbeat);
        clearInterval(hostPresencePoll);
        statusPillOffset.cleanup();
        sync.close();
        try {
            const queued = navigator.sendBeacon(
                api.apiURL(`/sonder-editor/session/${encodeURIComponent(projectId)}/release`),
                new Blob([JSON.stringify({ host_id: hostId, source_node_id: sourceNodeId, session_id: sessionId })], { type: "application/json" })
            );
            if (!queued) {
                releaseEditorSession(projectId, sessionId, false, hostId, sourceNodeId).catch(() => {});
            }
        } catch (_err) {
            releaseEditorSession(projectId, sessionId, false, hostId, sourceNodeId).catch(() => {});
        }
    });
}

main().catch((err) => {
    console.warn("[Sonder] Persistent editor tab failed:", err);
    setStatus(err?.message || String(err));
});
