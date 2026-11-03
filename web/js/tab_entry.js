import { installComfyApiShim } from "./comfy_api_shim.js";
import { FONT, THEME, applyNativeControlTheme, chromeButtonCss, injectSonderFontFaces, statusPillCss } from "./editor_theme.js";
import { mountToastStack } from "./editor_toast_stack.js";
import { configureNotifications, notify, notifySuccess, notifyWarning } from "./editor_notifications.js";
import { getEditorSettings, notificationCoreConfig, subscribeEditorSettings } from "./editor_settings.js";
import { createRunFeedbackDriver } from "./run_feedback_notice.js";
import { LAUNCHER_WINDOW_NAME } from "./sdk_setup_client.js";
import { modelMessage } from "./launcher_view.js";
import { createMountedControlsHost } from "./controls_host_remote.js";
import { createRunOutputsClient, createRunRequestClient } from "./run_outputs_client.js";
import { createMountedViewsHost, createSdkViewsHost } from "./views_hosts.js";
import { newSessionId } from "./session_ids.js";
import { createPageDisplacement, createRunReadDriver, heartbeatAction, mountedRefusalText, mountedSecret, ownerChangeAction, storedMountedSecret } from "./mounted_session.js";

installComfyApiShim();
injectSonderFontFaces();
applyNativeControlTheme(document.documentElement);

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
// The canvas workflow this mounted tab edits (`rootGraph.id`): its run reads
// and Output Views layout are keyed by it. Empty from an older handoff link.
const canvasWorkflowId = params.get("workflow_id") || "";
// The canvas Editor as a prompt key (root nodes only; "" in a subgraph).
const rootEditorKey = params.get("editor_key") || "";
// Sonder App (SDK mode): `?mode=sdk&session=<sdk session id>`; its secret is in sessionStorage.
const tabMode = params.get("mode") || "";
const sdkSessionId = params.get("session") || "";
const statusEl = document.getElementById("status");
// Fresh on every load, so the old page's release beacon can never release this one.
const sessionId = newSessionId("tab");

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
    take_placement_linked: true,
    take_placement_muted: false,
    take_fit_mode: "pad_edge",
    take_crop_position: "center",
    take_keep_padding: false,
};

function setStatus(message) {
    if (statusEl) statusEl.textContent = message;
}

function makeStatusPill() {
    const pill = document.createElement("div");
    pill.style.cssText = `
        ${statusPillCss({ state: "idle", padding: "4px 8px" })}
        font-size: 11px;
        pointer-events: none;
        flex: 0 0 auto;
        margin-right: 8px;
    `;
    const dot = document.createElement("span");
    dot.style.cssText = `
        width: 6px;
        height: 6px;
        border-radius: 999px;
        background: var(--sonder-status-color);
        flex: 0 0 auto;
    `;
    const label = document.createElement("span");
    label.textContent = "Connecting";
    pill._sonderLabel = label;
    pill.append(dot, label);
    // Mount inside the fullscreen toolbar's button cluster, left of Mount/Exit,
    // so status + session controls read as one group. The mounted tab enters
    // fullscreen before this runs, so the cluster already exists.
    const toolbarButtons = document.querySelector("[data-fs-toolbar-buttons]");
    if (toolbarButtons) {
        toolbarButtons.prepend(pill);
    } else {
        pill.style.position = "fixed";
        pill.style.top = "10px";
        pill.style.right = "12px";
        pill.style.zIndex = "10020";
        document.body.appendChild(pill);
    }
    return pill;
}

function setStatusPillState(pill, label, state = "idle") {
    const color = {
        running: THEME.statusRunning,
        pending: THEME.statusPending,
        failed: THEME.statusFailed,
        idle: THEME.statusIdle,
    }[state] || THEME.statusIdle;
    if (pill?._sonderLabel) {
        pill._sonderLabel.textContent = label;
    } else if (pill) {
        pill.textContent = label;
    }
    pill?.style?.setProperty?.("--sonder-status-color", color);
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

// Debounced self-echo guard (mutation-integrity F4): mirrors
// editor_node_controller._handleProjectUpdatedFromSync — the WS echo of an
// own save beats the response that records the new version, so re-check
// 250 ms later with the latest stashed event (re-armed per event). Shared by
// the mounted tab and the Sonder App, whose takes arrive this way.
function createProjectUpdatedHandler(editor, getProjectVersion) {
    let projectUpdatedDebounceTimer = null;
    let pendingProjectUpdatedEvent = null;
    const handler = (event) => {
        pendingProjectUpdatedEvent = event;
        clearTimeout(projectUpdatedDebounceTimer);
        projectUpdatedDebounceTimer = setTimeout(() => {
            projectUpdatedDebounceTimer = null;
            const pending = pendingProjectUpdatedEvent;
            pendingProjectUpdatedEvent = null;
            if (!pending) return;
            const currentVersion = getProjectVersion(projectId);
            const shouldRefresh = !!(pending.modified_at && pending.modified_at !== currentVersion);
            tabDiagRecord("project_updated_recv", {
                event_modified_at: pending.modified_at || "",
                current_version: currentVersion || "",
                refresh: shouldRefresh,
            });
            if (shouldRefresh) {
                editor.refresh(["project", "assets", "scenes", "queue", "references"]);
            }
        }, 250);
    };
    handler.cancel = () => {
        clearTimeout(projectUpdatedDebounceTimer);
        projectUpdatedDebounceTimer = null;
        pendingProjectUpdatedEvent = null;
    };
    return handler;
}

// The keys a displaced editor must no longer see. A focused field would still
// type natively, so it loses focus too.
function blockEditorKeys(register, PRIORITY) {
    document.activeElement?.blur?.();
    return register({
        id: "displaced-page",
        priority: PRIORITY.OVERLAY * 10,
        keydown: () => true,
        keyup: () => true,
        paste: () => true,
    });
}

function mountToastsOnce() {
    // Page-level notification toast stack, mounted once.
    if (!document.querySelector("[data-sonder-toast-stack]")) {
        mountToastStack(document.body);
    }
    configureNotifications(notificationCoreConfig());
    subscribeEditorSettings(() => configureNotifications(notificationCoreConfig()));
}

// A full-page message over the editor (or in place of it), with an optional
// action: a button (Take over) or a link (back to the Launcher).
function createPageBlocker() {
    const blocker = document.createElement("div");
    blocker.style.cssText = `
        position: fixed;
        inset: 0;
        z-index: 10010;
        display: none;
        align-items: center;
        justify-content: center;
        flex-direction: column;
        gap: 12px;
        background: ${THEME.bg0}cc;
        color: ${THEME.fg1};
        font: 13px ${FONT.sans};
        pointer-events: auto;
        text-align: center;
        padding: 16px;
        box-sizing: border-box;
    `;
    const message = document.createElement("div");
    message.style.cssText = `color:${THEME.fg0}; font-size:14px;`;
    const detail = document.createElement("div");
    detail.style.cssText = `color:${THEME.fg2}; font-size:12px; max-width:min(520px, 90vw); line-height:1.45;`;
    const actions = document.createElement("div");
    actions.style.cssText = "display:flex; gap:8px; flex-wrap:wrap; justify-content:center;";
    blocker.append(message, detail, actions);
    document.body.appendChild(blocker);
    return {
        show({ text, more = "", action = null, link = null }) {
            message.textContent = text;
            detail.textContent = more;
            detail.style.display = more ? "" : "none";
            actions.replaceChildren();
            if (action) {
                const button = document.createElement("button");
                button.type = "button";
                button.textContent = action.label;
                button.style.cssText = chromeButtonCss({ variant: "primary", padding: "6px 14px" });
                button.addEventListener("click", () => action.onClick(button));
                actions.appendChild(button);
            }
            if (link) {
                const anchor = document.createElement("a");
                anchor.href = link.href;
                anchor.textContent = link.label;
                anchor.style.cssText = `${chromeButtonCss({ variant: "secondary", padding: "6px 14px" })} text-decoration:none; display:inline-block;`;
                actions.appendChild(anchor);
            }
            blocker.style.display = "flex";
        },
        hide() {
            blocker.style.display = "none";
        },
    };
}

// ── Sonder App (SDK mode) ─────────────────────────────────────────────────
// Opened by the Launcher at `/sonder-editor/tab/<project folder>?mode=sdk&session=<id>`
// with the session secret in this tab's sessionStorage. The same editor as the
// mounted tab; the host differs: it runs the workflow itself (Generate) and
// keeps its position on the server. Every claim with the secret takes the
// session over (reload, duplicated tab, Take over); a displaced tab never
// re-claims on its own.
async function runSdkTab() {
    const [
        { connectProjectSync },
        { EditorWidget },
        { installProjectVersionFetchPatch, rememberProjectVersionFromPayload, getProjectVersion },
        { claimEditorSession, heartbeatEditorSession, releaseEditorSession },
        sdk,
        { register: registerKeyboardConsumer, PRIORITY: KEYBOARD_PRIORITY },
    ] = await Promise.all([
        import("./cross_tab_sync.js"),
        import("./editor_widget.js"),
        import("./api_client.js"),
        import("./editor_session_client.js"),
        import("./sdk_host.js"),
        import("./keyboard_ownership.js"),
    ]);

    const launcherHref = api.apiURL("/sonder-editor/app");
    const blocker = createPageBlocker();
    const showEnded = (more = "") => {
        document.body.classList.add("ready");
        blocker.show({
            text: "This session has ended",
            more: more || "Open the workflow and project again from the Launcher.",
            link: { href: launcherHref, label: "Back to the Launcher" },
        });
    };

    // The Launcher navigated this window here: give up its name, or the
    // editor's Launcher button would navigate this editor away.
    if (window.name === LAUNCHER_WINDOW_NAME) window.name = "";
    const secret = sdk.readSdkSecret(sdkSessionId);
    if (!sdkSessionId || !secret) {
        showEnded("This tab doesn't hold that session. Open it from the Launcher.");
        return;
    }
    tabDiagBootIfNeeded(sessionId);
    tabDiagRecord("sdk_tab_start", { project_id: projectId, sdk_session_id: sdkSessionId });

    installProjectVersionFetchPatch();

    const stateResp = await fetchJson(`/sonder-editor/sdk/session/${encodeURIComponent(sdkSessionId)}/state`, {
        headers: { [sdk.SDK_SECRET_HEADER]: secret },
    });
    if (!stateResp.ok) {
        sdk.forgetSdkSecret(sdkSessionId);
        showEnded();
        return;
    }
    // The session's own project key: an edited URL would claim under one key
    // while the server checks ownership under another.
    const sessionProject = String(stateResp.payload.project || "");
    if (sessionProject && sessionProject !== projectId) {
        window.location.replace(api.apiURL(
            `/sonder-editor/tab/${encodeURIComponent(sessionProject)}?mode=sdk&session=${encodeURIComponent(sdkSessionId)}`));
        return;
    }
    const workflowTitle = name => (name ? `${name} · Sonder App` : "Sonder App");
    document.title = workflowTitle(String(stateResp.payload.template_name || ""));

    const projectResp = await fetchJson(`/sonder-editor/project/${encodeURIComponent(projectId)}`);
    if (!projectResp.ok) {
        setStatus(`Project load failed (${projectResp.status}).`);
        return;
    }
    rememberProjectVersionFromPayload(projectResp.payload, projectId);

    const host = new sdk.SdkWidgetHost({
        sessionId: sdkSessionId,
        secret,
        projectId,
        tabSessionId: sessionId,
        templateId: String(stateResp.payload.template_id || ""),
        templateName: String(stateResp.payload.template_name || ""),
        defaults: DEFAULT_WIDGET_VALUES,
        initialValues: stateResp.payload.values || {},
        fetchImpl: (...args) => fetch(...args),
        apiURL: (path) => api.apiURL(path),
    });

    // Re-claims name the workflow the session runs now (it can switch).
    const claim = () => claimEditorSession(projectId, sessionId, "sdk", {
        host_id: sdkSessionId,
        workflow_label: host.templateName ? `Sonder App · ${host.templateName}` : "Sonder App",
        sdk_secret: secret,
    }, "", sdkSessionId);

    const firstClaim = await claim();
    if (!firstClaim.ok) {
        sdk.forgetSdkSecret(sdkSessionId);
        showEnded();
        return;
    }

    const node = projectWidgetNode();
    const editor = new EditorWidget(node, {
        host,
        hostMode: "sdk",
        generationHost: host,
        // Exit returns to the Launcher; takes still generating keep going and
        // the Launcher says so.
        onFullscreenExit: async () => {
            void host.flushValues();
            await host.drainSaves();
            sdk.writeSdkExitFlag({
                session_id: sdkSessionId,
                project: projectId,
                template_id: host.templateId,
                template_name: host.templateName,
            });
            window.location.assign(launcherHref);
        },
    });
    editor.updateProject(projectId);
    editor.applyWidgetState(host.values);
    // Observed outputs of this session's runs (data only; Output Views read it).
    editor.runOutputs = createRunOutputsClient({
        mode: "sdk", label: `sdk:${sdkSessionId}`, apiURL: path => api.apiURL(path),
        identity: () => ({
            projectId, sessionId: sdkSessionId, secret, tabSessionId: host.tabSessionId,
            runIds: () => host.status.runs.map(run => run.run_id),
            // Only the current workflow's runs (a switch replaces what the panes read).
            workflowId: host.templateId,
        }),
    });
    editor.runBranch = target => host.runBranch(target);
    const viewsHost = createSdkViewsHost({ host, runOutputs: () => editor.runOutputs, apiURL: path => api.apiURL(path) });
    editor.setViewsHost(viewsHost);
    editor._enterFullscreen();

    // Workflow ▾ switched the session: the project, scene and timeline stay;
    // the Controls panel remounts for the new workflow.
    host.onWorkflowChanged((change) => {
        tabDiagRecord("sdk_workflow_changed", { from: change.previous?.template_id, to: change.template_id });
        document.title = workflowTitle(change.name);
        editor.replaceControlsHost(host.getControlsHost());
        if (change.adopted) {
            notifyWarning(`This session now runs ${change.name || "another workflow"}`, {
                source: "sdk-workflow-switch",
                detail: "It was switched in another tab, or before this page heard back. The Controls now show it.",
            });
            return;
        }
        const notes = [];
        if (change.still_generating) {
            notes.push(`${change.still_generating} ${change.still_generating === 1 ? "take" : "takes"} still generating with ${change.previous?.name || "the previous workflow"}.`);
        }
        if (change.pending_jobs) {
            // Queued jobs record no workflow: each renders with the Generate that takes it.
            const jobs = `${change.pending_jobs} queued ${change.pending_jobs === 1 ? "job" : "jobs"}`;
            notes.push(change.still_generating
                ? `${jobs}: takes already generating may take some first; the rest render with ${change.name || "this workflow"}.`
                : `${jobs} will render with ${change.name || "this workflow"}.`);
        }
        notifySuccess(`Switched to ${change.name || "the workflow"}`, { source: "sdk-workflow-switch", detail: notes.join(" ") || undefined });
        void (async () => {
            // A preview only: Generate's own refusal remains the gate.
            const requirements = await fetchJson(`/sonder-editor/sdk/templates/${encodeURIComponent(change.template_id)}/requirements`);
            const missing = requirements.ok ? requirements.payload.missing_models || [] : [];
            if (missing.length) {
                notifyWarning(`${missing.length} ${missing.length === 1 ? "model is" : "models are"} missing for ${change.name || "this workflow"}`, {
                    source: "sdk-workflow-switch",
                    detail: missing.map(modelMessage).join("; "),
                });
            }
        })().catch(() => {});
    });
    document.body.classList.add("ready");
    mountToastsOnce();

    const statusPill = makeStatusPill();
    let transportState = "open";
    let socketOpenedOnce = false;

    const render = () => {
        let label = "Connected";
        let pillState = "running";
        if (host.ownerState === "ended") {
            label = "Session ended";
            pillState = "failed";
        } else if (host.ownerState === "displaced") {
            label = "Opened in another tab";
            pillState = "pending";
        } else if (transportState === "reconnecting" || transportState === "closed" || host.ownerState === "reconnecting") {
            label = "Reconnecting";
            pillState = "pending";
        } else if (host.ownerState === "orphaned") {
            label = "Reconnecting session";
            pillState = "pending";
        }
        setStatusPillState(statusPill, label, pillState);
        if (host.ownerState === "ended") {
            showEnded();
        } else if (host.ownerState === "displaced") {
            blocker.show({
                text: "Opened in another tab",
                more: "This workflow and project are open in another tab. Take over to continue here.",
                action: { label: "Take over", onClick: () => window.location.reload() },
                link: { href: launcherHref, label: "Back to the Launcher" },
            });
        } else {
            blocker.hide();
        }
    };

    const onProjectUpdated = createProjectUpdatedHandler(editor, getProjectVersion);
    const runReads = createRunReadDriver({
        refresh: (keys) => {
            tabDiagRecord("run_read", { keys });
            editor.refresh(keys);
        },
    });
    const displace = createPageDisplacement({
        blockKeys: () => blockEditorKeys(registerKeyboardConsumer, KEYBOARD_PRIORITY),
        stopWork: () => {
            ownership.stop();
            onProjectUpdated.cancel();
            runReads.stop();
            sync.close();
        },
        drain: () => editor._drainProjectMutations?.("sdk_tab_displaced"),
        destroyEditor: () => {
            editor.destroy();
            host.destroy();
        },
        show: () => {},  // render() draws it, on the same state change
    });

    // The state machine lives in sdk_host.js (createSdkOwnership); this page
    // only draws it.
    const ownership = sdk.createSdkOwnership({
        host,
        sessionTag: firstClaim.session_tag || "",
        claim,
        heartbeat: () => heartbeatEditorSession(projectId, sessionId, sdkSessionId, ""),
        onChange: (state, previous) => {
            tabDiagRecord("sdk_owner_state", { previous, state });
            if (state === "ended") sdk.forgetSdkSecret(sdkSessionId);
            // Output Views stop reading while this tab does not own the session.
            if (state === "active") viewsHost.restored();
            else if (["ended", "displaced", "orphaned"].includes(state)) viewsHost.lost();
            render();
            if (state === "displaced") displace();
        },
        // A taken-over session reads its running takes' receipts too: they are
        // broadcast only when they change.
        onReclaimed: () => {
            void host.refreshStatus();
            void host.refreshRunFeedback();
        },
    });

    host.onOwnershipLost = (code) => void ownership.handleOwnershipLost(code);
    ownership.start(firstClaim.owner?.status || "active");

    const sync = connectProjectSync(projectId, {
        onProjectUpdated,
        onGenerationStatus: (event) => {
            host.applyStatus(event);
        },
        onRunFinished: (event) => runReads.runFinished(event),
        onRunProgress: (event) => host.applyRunProgress(event),
        onSessionChanged: (event) => {
            if (String(event.host_id || "") !== sdkSessionId) return;
            ownership.handleSessionChanged(event);
        },
        onConnectionState: (state) => {
            const reconnected = state === "open" && socketOpenedOnce;
            if (state === "open") socketOpenedOnce = true;
            transportState = state;
            tabDiagRecord("transport_state", { state });
            render();
            runReads.connection(state);
            // Snapshots sent while the socket was down are gone; fetch the latest.
            if (reconnected) {
                void host.refreshStatus();
                void host.refreshRunFeedback();
            }
        },
    }, {
        clientId: sessionId,
        hostId: sdkSessionId,
        sessionId,
        workflowLabel: "Sonder App",
    });

    // Runs still generating from an earlier visit (reattach, reload).
    void host.refreshStatus();
    void host.refreshRunFeedback();

    // Save the position while the page can still finish a request.
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden" && !displace.isDisplaced()) void host.flushValues();
    });

    // Restored from the back/forward cache: pagehide already released the
    // claim and stopped the host, so start afresh (the reload re-claims). Not a
    // displaced page: its reload would take the session back on its own.
    window.addEventListener("pageshow", (event) => {
        if (event.persisted && !displace.isDisplaced()) window.location.reload();
    });

    window.addEventListener("pagehide", () => {
        if (displace.isDisplaced()) return;
        ownership.stop();
        const savingValues = host.hasUnsavedValues();
        void host.flushValues({ keepalive: true });
        try {
            editor?._drainProjectMutations?.("sdk_tab_pagehide").catch(() => {});
        } catch (_err) {}
        sync.close();
        host.destroy();
        // Released like a closing mounted tab, so housekeeping can drop an
        // abandoned session; reopening from the Launcher reattaches. Not while
        // a last save is in flight: a release processed first would refuse it
        // (not_owner). The owner then lapses to orphaned, which housekeeping
        // also drops, and a reopen takes it over.
        if (host.ownerState === "ended" || savingValues) return;
        try {
            const queued = navigator.sendBeacon(
                api.apiURL(`/sonder-editor/session/${encodeURIComponent(projectId)}/release`),
                new Blob([JSON.stringify({ host_id: sdkSessionId, source_node_id: "", session_id: sessionId })], { type: "application/json" })
            );
            if (!queued) releaseEditorSession(projectId, sessionId, false, sdkSessionId, "").catch(() => {});
        } catch (_err) {
            releaseEditorSession(projectId, sessionId, false, sdkSessionId, "").catch(() => {});
        }
    });
}

async function main() {
    if (!projectId) {
        setStatus("Missing project id.");
        return;
    }
    if (tabMode === "sdk") {
        await runSdkTab();
        return;
    }
    const tabStorage = (() => {
        try {
            return window.sessionStorage;
        } catch (_err) {
            return null;
        }
    })();
    if (!handoffToken && !storedMountedSecret(tabStorage)) {
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

    // The handoff and this tab's secret go together: a reload's token is spent, and
    // its secret is what reclaims. Claimed before the widget state is read, because
    // a reclaim is what restores those values.
    const claim = await claimEditorSession(projectId, sessionId, "tab", {
        host_id: hostId,
        source_node_id: sourceNodeId,
        browser_instance_id: sessionWindowName,
        workflow_label: "Persistent editor tab",
        reclaim_secret: mountedSecret(tabStorage),
    }, handoffToken, hostId);
    if (!claim.ok) {
        setStatus(mountedRefusalText(claim.owner));
        return;
    }
    const sessionTag = claim.session_tag || "";

    // Nothing else holds this claim yet: a tab that can't go on gives it back, and
    // its values wait for a reload. It must not start on defaults and publish them.
    let initialState;
    try {
        initialState = await getEditorWidgetState(projectId, sourceNodeId, hostId);
    } catch (err) {
        await releaseEditorSession(projectId, sessionId, false, hostId, sourceNodeId).catch(() => {});
        setStatus("The editor could not load. Reload this tab to try again.");
        return;
    }
    const host = new TabWidgetHost(projectId, hostId, sourceNodeId, sessionId, initialState.values || initialState.state || {});
    host.setCanvasConnected(!!initialState.canvas_host_connected);
    host.setSessionStatus(claim.owner?.status || "active");
    host.setPublishingEnabled(true);

    const controlsHost = createMountedControlsHost({ projectId, hostId, sessionId,
        apiURL: path => api.apiURL(path),
        writeBlock: () => {
            if (!host.canvasHostConnected) return "canvas";
            if (host.sessionStatus === "active") return "";
            return host.sessionStatus === "orphaned" || host.sessionStatus === "reconnecting" ? "reconnecting" : "session";
        },
    });
    const node = projectWidgetNode();
    const editor = new EditorWidget(node, {
        host, controlsHost,
        hostMode: "tab",
        onFullscreenExit: () => window.close(),
        // The tab is named after its project, as the Sonder App names its workflow.
        onProjectIdentityChanged: name => {
            document.title = name ? `${name} · Sonder Editor` : "Sonder Editor";
        },
    });
    editor.updateProject(projectId);
    editor.applyWidgetState(host.values);
    // Observed outputs of its canvas host's runs (data only; Output Views read it),
    // and branch runs through the canvas host's run lane.
    editor.runOutputs = createRunOutputsClient({
        mode: "mounted", label: `mounted:${hostId}`, apiURL: path => api.apiURL(path),
        // The canvas workflow's runs only, as the canvas page reads them.
        identity: () => ({ projectId, hostId, sessionId, workflowId: canvasWorkflowId }),
    });
    const runRequests = createRunRequestClient({
        apiURL: path => api.apiURL(path), identity: () => ({ projectId, hostId, sessionId }),
        // The panel's focused edits first, then the posted writes, as Generate.
        flush: async () => (await editor.flushControlsPanel()) !== false && controlsHost.flush(),
    });
    editor.runBranch = target => runRequests.runBranch(target);
    const viewsHost = createMountedViewsHost({
        workflowId: canvasWorkflowId, editorKey: rootEditorKey, runOutputs: () => editor.runOutputs, runRequests: () => runRequests,
        controlsHost: () => controlsHost, apiURL: path => api.apiURL(path),
    });
    editor.setViewsHost(viewsHost);
    editor._enterFullscreen();
    document.body.classList.add("ready");
    mountToastsOnce();
    if (sessionWindowName) {
        try {
            window.name = sessionWindowName;
        } catch (_err) {}
    }

    const statusPill = makeStatusPill();
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
        background: ${THEME.bg0}cc;
        color: ${THEME.statusPending};
        font: 13px ${FONT.sans};
        pointer-events: auto;
    `;
    const blockerMessage = document.createElement("div");
    blockerMessage.textContent = "Canvas not connected";
    const blockerDebug = document.createElement("div");
    blockerDebug.style.cssText = `
        max-width: min(720px, 80vw);
        color: ${THEME.fg2};
        font-size: 11px;
        line-height: 1.4;
        text-align: center;
        word-break: break-word;
    `;
    blocker.append(blockerMessage, blockerDebug);
    document.body.appendChild(blocker);

    const tabDebugEnabled = window.SONDER_DEBUG_TAB === true;
    blockerDebug.style.display = tabDebugEnabled ? "" : "none";

    // Another tab took this session over (a duplicated tab).
    const displace = createPageDisplacement({
        blockKeys: () => blockEditorKeys(registerKeyboardConsumer, KEYBOARD_PRIORITY),
        stopWork: () => {
            host.setPublishingEnabled(false);
            stopPageWork();
        },
        drain: () => editor._drainProjectMutations?.("tab_displaced"),
        destroyEditor: () => editor.destroy(),
        show: () => {
            setStatus("");
            document.body.classList.add("ready");
            blocker.style.display = "none";
            createPageBlocker().show({
                text: "Opened in another tab",
                more: "This editor is open in another tab. Take over to continue here.",
                action: { label: "Take over", onClick: () => window.location.reload() },
            });
        },
    });

    const updateBlocker = () => {
        if (displace.isDisplaced()) return;
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
        const live = host.canvasHostConnected && host.sessionStatus === "active";
        blocker.style.display = live ? "none" : "flex";
        // Output Views stop reading while the tab is orphaned or its canvas is gone.
        if (live) viewsHost.restored();
        else viewsHost.lost();
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
        if (displace.isDisplaced()) return;
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
        if (displace.isDisplaced()) return;
        if (state) transportState = state;
        let label = "Canvas connected";
        let pillState = "running";
        if (transportState === "reconnecting" || transportState === "closed") {
            label = "Reconnecting";
            pillState = "pending";
        } else if (!host.canvasHostConnected) {
            label = "Canvas not connected";
            pillState = "pending";
        } else if (host.sessionStatus === "orphaned") {
            label = "Reconnecting session";
            pillState = "pending";
        } else if (host.sessionStatus === "released") {
            label = "Session released";
            pillState = "failed";
        } else if (host.sessionStatus === "reconnecting") {
            label = "Reconnecting session";
            pillState = "pending";
        } else if (host.sessionStatus !== "active") {
            label = "Session not active";
            pillState = "pending";
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
        setStatusPillState(statusPill, label, pillState);
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
            // The tab's own state first: the Controls host reports only while it may write.
            const changed = setCanvasConnected(!!state.canvas_host_connected);
            if (changed) updatePill();
            controlsHost.presence(state);
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
                owner_session_tag: result.owner?.session_tag || "",
                canvas_host_connected: !!result.canvas_host_connected,
            });
            if (displace.isDisplaced()) return;
            const action = heartbeatAction(result);
            if (action === "own") {
                setSessionStatus(result.owner?.status || "active");
                setStatus("");
                updatePill("open");
            } else if (action === "displaced") {
                displace();
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
            if (displace.isDisplaced()) return;
            setSessionStatus("reconnecting");
            updatePill("reconnecting");
        });
    };
    sendSessionHeartbeat();
    const heartbeat = setInterval(sendSessionHeartbeat, 10000);

    document.addEventListener("visibilitychange", () => {
        tabDiagRecord("visibilitychange", { state: document.visibilityState });
    });

    // The canvas host's run notifications, shown here too: the records it owns
    // reach this tab over the project socket, and the read restores them after a
    // reload or a reconnect.
    const runFeedback = createRunFeedbackDriver({
        page: "mounted",
        notify,
        timecodeMode: () => getEditorSettings()?.timelineBehavior?.timecodeMode || "frames",
        ownerMatch: (record) => Array.isArray(record?.host_ids) && record.host_ids.includes(hostId),
    });
    // Retried while it fails (after a restart the gate refuses until the canvas
    // host is back); a read that never answers ends what the tab was showing.
    const readRunFeedback = () => runFeedback.read(async () => {
        const query = `host_id=${encodeURIComponent(hostId)}&session_id=${encodeURIComponent(sessionId)}`;
        const result = await fetchJson(`/sonder-editor/project/${encodeURIComponent(projectId)}/runs/feedback?${query}`).catch(() => null);
        return result?.ok ? result.payload : null;
    }, { project: projectId });

    const onProjectUpdated = createProjectUpdatedHandler(editor, getProjectVersion);
    const runReads = createRunReadDriver({
        refresh: (keys) => {
            tabDiagRecord("run_read", { keys });
            editor.refresh(keys);
        },
    });
    const sync = connectProjectSync(projectId, {
        onProjectUpdated,
        onRunFinished: (event) => runReads.runFinished(event),
        onRunProgress: (event) => runFeedback.applyRecord(event?.record, { project: event?.project_id }),
        onWidgetStateChanged: (event) => {
            if (String(event.host_id || "") !== hostId) return;
            if (String(event.source_node_id || "") !== sourceNodeId) return;
            if (sessionTag && event.source_tag === sessionTag) return;
            const values = event.values && typeof event.values === "object" ? event.values : {};
            host.apply(values);
            editor.applyWidgetState(values);
        },
        onControlsChanged: (event) => controlsHost.changed(event),
        onHostPresenceChanged: (event) => {
            // The tab's own state first: the Controls host reports only while it may write.
            if (String(event.host_id || "") === hostId && String(event.source_node_id || "") === sourceNodeId
                && setCanvasConnected(!!event.canvas_host_connected)) {
                updatePill();
            }
            controlsHost.presence(event);
        },
        onSubscribed: (event) => {
            void controlsHost.refresh(true);
            void readRunFeedback();
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
            if (displace.isDisplaced()) return;
            const action = ownerChangeAction(owner, sessionTag);
            if (action === "own") {
                setSessionStatus(owner.status || "active");
                if ((owner.status || "active") === "active") {
                    setStatus("");
                }
                updatePill();
            } else if (action === "displaced") {
                displace();
            } else {
                setStatus("Editor tab session was released.");
                setSessionStatus("released");
                updatePill();
            }
        },
        onConnectionState: (state) => {
            tabDiagRecord("transport_state", { state });
            updatePill(state);
            runReads.connection(state);
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

    function stopPageWork() {
        if (diagHotkeyUnregister) {
            try { diagHotkeyUnregister(); } catch (_) {}
            diagHotkeyUnregister = null;
        }
        clearInterval(heartbeat);
        clearInterval(hostPresencePoll);
        controlsHost.destroy();
        onProjectUpdated.cancel();
        runReads.stop();
        runFeedback.destroy();
        sync.close();
    }

    // Restored from the back/forward cache: pagehide already released the claim
    // and stopped this page, so start afresh (the reload reclaims). Not a displaced
    // page: its reload would take the session back on its own.
    window.addEventListener("pageshow", (event) => {
        if (event.persisted && !displace.isDisplaced()) window.location.reload();
    });

    window.addEventListener("pagehide", () => {
        // A displaced page owns nothing, and already stopped.
        if (displace.isDisplaced()) return;
        try {
            editor?._drainProjectMutations?.("tab_pagehide").catch(() => {});
        } catch (_err) {}
        stopPageWork();
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
