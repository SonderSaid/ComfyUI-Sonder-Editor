import { registerChangeCertificateFromEvent } from "./api_client.js";
import { proxyStore } from "./media_display_source.js";
import { refreshProxyStates, requestProxyReconcile } from "./proxy_client.js";
import { publishRunEvent } from "./run_outputs_client.js";

const { api } = window.comfyAPI.api;

function wsURL(path) {
    const url = new URL(api.apiURL(path), window.location.href);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    return url.toString();
}

export function connectProjectSync(projectId, handlers = {}, options = {}) {
    let ws = null;
    let closed = false;
    let reconnectTimer = null;
    let hostHeartbeatTimer = null;
    let attempt = 0;
    let openedBefore = false;
    const encodedProjectId = encodeURIComponent(projectId || "");
    const clientId = String(options.clientId || options.hostId || options.sessionId || "").trim();
    const encodedClientId = encodeURIComponent(clientId);

    const emitState = (state, details = {}) => {
        try {
            handlers.onConnectionState?.(state, details);
        } catch (err) {
            console.warn("[Sonder] sync connection state handler failed:", err);
        }
    };

    const dispatch = (event) => {
        if (!event || typeof event !== "object") return;
        try {
            if (event.type === "project_updated") {
                // Registered on receipt, before any debounce: the HTTP response that
                // announces the same version may arrive later, and a consumer told
                // of that version must find the certificate already held.
                registerChangeCertificateFromEvent(event);
                handlers.onProjectUpdated?.(event);
            } else if (event.type === "widget_state_changed") handlers.onWidgetStateChanged?.(event);
            else if (event.type === "session_changed") handlers.onSessionChanged?.(event);
            else if (event.type === "host_presence_changed") handlers.onHostPresenceChanged?.(event);
            else if (event.type === "subscribed") handlers.onSubscribed?.(event);
            // Sent once per ComfyUI prompt that touched this project, after Sonder
            // finished its bridges and returned any job the run left running.
            else if (event.type === "run_finished") handlers.onRunFinished?.(event);
            // Sonder App run status, about 5 Hz while takes generate. Ordered by
            // the session's `seq`; the host drops stale snapshots (sdk_host.js).
            else if (event.type === "generation_status") handlers.onGenerationStatus?.(event);
            // Proxy builds (server/proxy_media.py), batched per project. Every page
            // that syncs a project keeps the shared store current, whichever mode
            // it is; handlers only repaint.
            else if (event.type === "proxy_status") {
                proxyStore.ingestItems(projectId, event.items || []);
                proxyStore.ingestQueue(projectId, event.queue, event.epoch);
                handlers.onProxyStatus?.(event);
            }
            // Observed run outputs (server/run_outputs.py): ids only, no media.
            // Fed to the shared hint bus; each host's client picks its own.
            else if (event.type === "outputs_changed") {
                publishRunEvent(projectId, event);
                handlers.onOutputsChanged?.(event);
            }
            // A run recorded a live sampling preview (at most every 250 ms per
            // run): ids only; sampling panes read runs/live again.
            else if (event.type === "live_preview") publishRunEvent(projectId, event);
            // Mounted run lane (session_registry): the canvas claims, the tab
            // rereads its request's outcome.
            else if (event.type === "run_requests_pending") handlers.onRunRequestsPending?.(event);
            else if (event.type === "run_request_changed") {
                publishRunEvent(projectId, event);
                handlers.onRunRequestChanged?.(event);
            }
            else if (event.type === "controls_changed") handlers.onControlsChanged?.(event);
            else if (event.type === "control_changes_pending") handlers.onControlChangesPending?.(event);
            else if (event.type === "controls_visibility_changed") handlers.onControlsVisibilityChanged?.(event);
        } catch (err) {
            console.warn("[Sonder] sync event handler failed:", err);
        }
    };

    const subscribe = () => {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        ws.send(JSON.stringify({
            type: "subscribe",
            project_id: projectId,
            role: options.role || "",
            host_id: options.hostId || "",
            source_node_id: options.sourceNodeId || "",
            session_id: options.sessionId || "",
            workflow_id: options.workflowId || "",
            workflow_label: options.workflowLabel || "",
        }));
    };

    const stopHostHeartbeat = () => {
        clearInterval(hostHeartbeatTimer);
        hostHeartbeatTimer = null;
    };

    const startHostHeartbeat = () => {
        stopHostHeartbeat();
        if (options.role !== "canvas_host") return;
        hostHeartbeatTimer = setInterval(() => {
            if (!ws || ws.readyState !== WebSocket.OPEN) return;
            ws.send(JSON.stringify({ type: "host_heartbeat" }));
        }, 10000);
    };

    const scheduleReconnect = () => {
        if (closed) return;
        emitState("reconnecting");
        const delay = Math.min(30000, 1000 * Math.pow(2, attempt++));
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, delay);
    };

    function connect() {
        if (closed || !projectId || !clientId) return;
        const url = wsURL(`/sonder-editor/ws?project_id=${encodedProjectId}&client_id=${encodedClientId}&clientId=${encodedClientId}`);
        emitState(attempt ? "reconnecting" : "connecting", { url });
        try {
            ws = new WebSocket(url);
        } catch (err) {
            console.warn("[Sonder] sync socket creation failed:", err);
            scheduleReconnect();
            return;
        }
        ws.onopen = () => {
            attempt = 0;
            emitState("open");
            subscribe();
            startHostHeartbeat();
            // Proxy events sent while the socket was down are gone: read the state.
            // A server restart also lost its build queue: have it reconcile.
            if (openedBefore) {
                void refreshProxyStates(projectId);
                requestProxyReconcile(projectId);
            }
            openedBefore = true;
        };
        ws.onmessage = (message) => {
            let event = null;
            try {
                event = JSON.parse(message.data);
            } catch (_err) {
                return;
            }
            dispatch(event);
        };
        ws.onerror = () => {};
        ws.onclose = () => {
            ws = null;
            stopHostHeartbeat();
            scheduleReconnect();
        };
    }

    connect();

    return {
        close() {
            closed = true;
            clearTimeout(reconnectTimer);
            reconnectTimer = null;
            stopHostHeartbeat();
            try {
                // A deliberate departure ends the canvas host now. A bare transport
                // loss keeps it until its TTL, so pending Controls survive an outage.
                if (options.role === "canvas_host" && ws?.readyState === WebSocket.OPEN) {
                    ws.send(JSON.stringify({ type: "unregister_host" }));
                }
                ws?.close();
            } catch (_err) {}
            ws = null;
            emitState("closed");
        },
        send(message) {
            if (ws?.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify(message || {}));
                return true;
            }
            return false;
        },
    };
}
