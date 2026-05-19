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
    const encodedProjectId = encodeURIComponent(projectId || "");

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
            if (event.type === "project_updated") handlers.onProjectUpdated?.(event);
            else if (event.type === "widget_state_changed") handlers.onWidgetStateChanged?.(event);
            else if (event.type === "session_changed") handlers.onSessionChanged?.(event);
            else if (event.type === "host_presence_changed") handlers.onHostPresenceChanged?.(event);
            else if (event.type === "subscribed") handlers.onSubscribed?.(event);
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
        if (closed || !projectId) return;
        const url = wsURL(`/sonder-editor/ws?project_id=${encodedProjectId}`);
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
