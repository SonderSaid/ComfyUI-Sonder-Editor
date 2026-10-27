// Sonder App (SDK mode) host: the editor's widget host for one SDK session,
// and the Generate API the toolbar drives.
//
// The tab that owns the session holds its secret in sessionStorage (never in
// the URL). Editor position values are saved per project on the server
// (`PUT …/values`, debounced); Generate sends the current values with it, so a
// pending save is folded into the Generate call. Run status arrives as
// `generation_status` snapshots, websocket or `GET …/status`, ordered by `seq`.
//
// No DOM here: tab_entry.js owns the page, generate_controls.js the toolbar.

import { createSdkControlsHost } from "./controls_host_remote.js";

export const SDK_SECRET_KEY_PREFIX = "sonder-sdk-session:";
export const SDK_EXIT_FLAG_KEY = "sonder-sdk-exit";
export const SDK_SECRET_HEADER = "X-Sonder-SDK-Session";
export const SDK_TAB_HEADER = "X-Sonder-Tab-Session";
export const SDK_VALUES_DEBOUNCE_MS = 400;
export const SDK_MAX_COUNT = 8;

const UNFINISHED_STATES = new Set(["submitting", "unconfirmed", "queued", "running", "finishing"]);

export function isUnfinishedRun(run) {
    return UNFINISHED_STATES.has(String(run?.state || ""));
}

function sessionStore(store) {
    if (store !== undefined) return store;
    try {
        return globalThis.sessionStorage || null;
    } catch (_err) {
        return null;
    }
}

export function storeSdkSecret(sessionId, secret, store) {
    const target = sessionStore(store);
    if (!target || !sessionId) return false;
    try {
        target.setItem(`${SDK_SECRET_KEY_PREFIX}${sessionId}`, String(secret || ""));
        return true;
    } catch (_err) {
        return false;
    }
}

export function readSdkSecret(sessionId, store) {
    const target = sessionStore(store);
    if (!target || !sessionId) return "";
    try {
        return String(target.getItem(`${SDK_SECRET_KEY_PREFIX}${sessionId}`) || "");
    } catch (_err) {
        return "";
    }
}

export function forgetSdkSecret(sessionId, store) {
    const target = sessionStore(store);
    if (!target || !sessionId) return;
    try {
        target.removeItem(`${SDK_SECRET_KEY_PREFIX}${sessionId}`);
    } catch (_err) {}
}

// Set by Exit in SDK mode; read once by the Launcher to say what is still
// generating. Holds no secret: the Launcher reads the secret by session id.
export function writeSdkExitFlag(flag, store) {
    const target = sessionStore(store);
    if (!target) return;
    try {
        target.setItem(SDK_EXIT_FLAG_KEY, JSON.stringify({ ...(flag || {}), at: Date.now() }));
    } catch (_err) {}
}

export function takeSdkExitFlag(store) {
    const target = sessionStore(store);
    if (!target) return null;
    try {
        const raw = target.getItem(SDK_EXIT_FLAG_KEY);
        target.removeItem(SDK_EXIT_FLAG_KEY);
        const flag = raw ? JSON.parse(raw) : null;
        return flag && typeof flag === "object" && flag.session_id ? flag : null;
    } catch (_err) {
        return null;
    }
}

async function readResponse(resp) {
    let payload = {};
    try {
        payload = await resp.json();
    } catch (_err) {
        payload = {};
    }
    return { ok: !!resp.ok, status: resp.status, payload: payload && typeof payload === "object" ? payload : {} };
}

// Owner states: "active" and "orphaned" may write; "displaced" (another tab
// took the session over) and "ended" (the server no longer knows it) may not.
const WRITABLE_OWNER_STATES = new Set(["active", "orphaned", "reconnecting"]);

export class SdkWidgetHost {
    constructor({
        sessionId,
        secret,
        projectId,
        tabSessionId,
        templateId = "",
        templateName = "",
        defaults = {},
        initialValues = {},
        fetchImpl = (...args) => globalThis.fetch(...args),
        apiURL = (path) => path,
        setTimeoutImpl = (fn, ms) => globalThis.setTimeout(fn, ms),
        clearTimeoutImpl = (id) => globalThis.clearTimeout(id),
        debounceMs = SDK_VALUES_DEBOUNCE_MS,
    } = {}) {
        this.sessionId = String(sessionId || "");
        this.secret = String(secret || "");
        this.projectId = String(projectId || "");
        this.tabSessionId = String(tabSessionId || "");
        // The workflow this page believes the session runs. Every Generate and
        // Controls write captures it when called; the server refuses one that
        // no longer matches (`workflow_changed`).
        this.templateId = String(templateId || "");
        this.templateName = String(templateName || "");
        this.values = { ...(defaults || {}), ...(initialValues || {}) };
        this.ownerState = "active";
        this.status = { seq: -1, runs: [] };
        this.details = new Map();
        this.onOwnershipLost = null;
        this._fetch = fetchImpl;
        this._apiURL = apiURL;
        this._setTimeout = setTimeoutImpl;
        this._clearTimeout = clearTimeoutImpl;
        this._debounceMs = debounceMs;
        this._dirty = {};
        this._flushTimer = null;
        this._inflightSaves = new Set();
        this._seq = 0;
        this._cancelEpoch = 0;
        this._listeners = new Set();
        this._feedbackListeners = new Set();
        this._lastFeedbackRead = null;
        this._workflowListeners = new Set();
        this._generating = 0;
        this._switching = false;
        this._controlsHost = null;
        this.flushControlsPanel = null;
        this._destroyed = false;
    }

    // ── EditorWidget host contract ─────────────────────────────────────────

    getControlsHost() {
        if (!this._controlsHost) this._controlsHost = createSdkControlsHost(this);
        return this._controlsHost;
    }

    getValue(name, defaultValue = 0) {
        return Object.prototype.hasOwnProperty.call(this.values, name) ? this.values[name] : defaultValue;
    }

    setValue(name, value) {
        if (Object.is(this.values[name], value)) return;
        this.values[name] = value;
        this._dirty[name] = value;
        this._scheduleFlush();
    }

    setValueLocal(name, value) {
        if (Object.is(this.values[name], value)) return;
        this.values[name] = value;
    }

    getNodeId() {
        return this.sessionId || "sdk";
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

    // ── Ownership ──────────────────────────────────────────────────────────

    canWrite() {
        return !this._destroyed && WRITABLE_OWNER_STATES.has(this.ownerState);
    }

    setOwnerState(state) {
        const previous = this.ownerState;
        this.ownerState = String(state || "");
        if (!this.canWrite()) this.details.clear();
        if (this.canWrite() && Object.keys(this._dirty).length) this._scheduleFlush();
        // The server no longer knows this session, so no snapshot will ever
        // finish its runs: publish a terminal one so progress stops.
        if (this.ownerState === "ended" && previous !== "ended") {
            this._publish({ seq: this.status.seq, template_name: this.status.template_name || "", runs: [], ended: true });
        }
    }

    setTabSessionId(tabSessionId) {
        this.tabSessionId = String(tabSessionId || "");
    }

    headers(json = true) {
        const headers = {
            [SDK_SECRET_HEADER]: this.secret,
            [SDK_TAB_HEADER]: this.tabSessionId,
        };
        if (json) headers["Content-Type"] = "application/json";
        return headers;
    }

    _sessionPath(suffix) {
        return this._apiURL(`/sonder-editor/sdk/session/${encodeURIComponent(this.sessionId)}/${suffix}`);
    }

    _noteRefusal(result) {
        const code = String(result?.payload?.code || "");
        if (code === "not_owner" || code === "session_ended") {
            this.onOwnershipLost?.(code);
        }
        // The session runs another workflow than this page believes (switched
        // from another tab, or a switch whose answer was lost): read it back.
        if (code === "workflow_changed" && !this._switching) void this.refreshStatus();
        return code;
    }

    // ── Editor position (per project, saved on the server) ─────────────────

    _scheduleFlush() {
        if (!this.canWrite()) return;
        if (this._flushTimer != null) this._clearTimeout(this._flushTimer);
        this._flushTimer = this._setTimeout(() => {
            this._flushTimer = null;
            void this.flushValues();
        }, this._debounceMs);
    }

    _takeDirty() {
        if (this._flushTimer != null) {
            this._clearTimeout(this._flushTimer);
            this._flushTimer = null;
        }
        const dirty = this._dirty;
        this._dirty = {};
        return dirty;
    }

    // Put back edits a request did not save; newer edits win. A failed save is
    // not retried on a timer (offline or displaced, that would loop): the next
    // edit, regained ownership or Exit sends it.
    _restoreDirty(dirty, { schedule = false } = {}) {
        if (!Object.keys(dirty).length) return;
        this._dirty = { ...dirty, ...this._dirty };
        if (schedule) this._scheduleFlush();
    }

    hasUnsavedValues() {
        return Object.keys(this._dirty).length > 0;
    }

    // `keepalive` lets a save started from pagehide outlive the page (sendBeacon
    // cannot carry the session headers).
    // A refused save keeps its edits for the next one, except when the edits
    // themselves were refused (400) or the session is gone (404). A 409 means
    // another tab owns the session: the edits wait for Take over.
    async flushValues({ keepalive = false } = {}) {
        if (!this.canWrite()) return false;
        const dirty = this._takeDirty();
        if (!Object.keys(dirty).length) return true;
        const request = (async () => {
            try {
                const result = await readResponse(await this._fetch(this._sessionPath("values"), {
                    method: "PUT",
                    headers: this.headers(),
                    body: JSON.stringify({ values: { ...this.values }, seq: ++this._seq }),
                    ...(keepalive ? { keepalive: true } : {}),
                }));
                if (!result.ok) {
                    if (result.status === 409 || result.status >= 500) this._restoreDirty(dirty);
                    this._noteRefusal(result);
                }
                return result.ok;
            } catch (err) {
                // Offline: keep the change for the next save.
                this._restoreDirty(dirty);
                console.warn("[Sonder] Saving the Sonder App editor position failed:", err);
                return false;
            }
        })();
        this._inflightSaves.add(request);
        try {
            return await request;
        } finally {
            this._inflightSaves.delete(request);
        }
    }

    // Bound navigation/Generate waiting, while server sequences protect even
    // a save that outlives the timeout. Recheck the set as saves can start while
    // another request is settling. Pending dirty values remain the caller's.
    async drainSaves({ timeoutMs = 10000 } = {}) {
        let timer;
        let expired = false;
        const timeout = new Promise((resolve) => {
            timer = this._setTimeout(() => { expired = true; resolve(false); }, timeoutMs);
        });
        try {
            while (this._inflightSaves.size && !expired) {
                const settled = Promise.allSettled([...this._inflightSaves]).then(() => true);
                if (!await Promise.race([settled, timeout])) return false;
            }
            return !expired;
        } finally {
            this._clearTimeout(timer);
        }
    }

    // ── Generate API (EditorWidget `generationHost`) ───────────────────────

    subscribe(listener) {
        if (typeof listener !== "function") return () => {};
        this._listeners.add(listener);
        try {
            listener(this.status);
        } catch (err) {
            console.warn("[Sonder] generation status listener failed:", err);
        }
        return () => this._listeners.delete(listener);
    }

    // A websocket snapshot must be strictly newer; a fetched one may repeat the
    // last seq (the server stamps seq only when it emits).
    applyStatus(snapshot, { fetched = false } = {}) {
        if (!snapshot || typeof snapshot !== "object") return false;
        if (String(snapshot.session_id || "") !== this.sessionId) return false;
        const seq = Number(snapshot.seq);
        if (!Number.isFinite(seq)) return false;
        // Failure details are immutable and arrive only on owner-gated reads.
        // A newer websocket snapshot must not discard this fetched detail.
        if (fetched && this.canWrite() && snapshot.details && typeof snapshot.details === "object") {
            for (const [id, detail] of Object.entries(snapshot.details)) {
                if (typeof detail === "string") this.details.set(id, detail);
            }
        }
        if (fetched ? seq < this.status.seq : seq <= this.status.seq) return false;
        const ids = new Set((snapshot.runs || []).map(run => run.run_id));
        for (const id of this.details.keys()) if (!ids.has(id)) this.details.delete(id);
        if (this.ownerState === "ended") return false;
        const templateId = String(snapshot.template_id || "");
        if (templateId && templateId !== this.templateId && !this._switching) {
            this._adoptWorkflow(templateId, String(snapshot.template_name || ""));
        }
        this._publish({
            seq,
            template_name: String(snapshot.template_name || ""),
            runs: Array.isArray(snapshot.runs) ? snapshot.runs.map((run) => ({ ...run })) : [],
            fetched: !!fetched,
        });
        return true;
    }

    _publish(status) {
        this.status = status;
        for (const listener of [...this._listeners]) {
            try {
                listener(this.status);
            } catch (err) {
                console.warn("[Sonder] generation status listener failed:", err);
            }
        }
    }

    activeRuns() {
        return this.status.runs.filter(isUnfinishedRun);
    }

    // ── Run feedback (stage, count, receipt; server/run_feedback.py) ────────
    // The App's runs keep their state and outcome in generation status; these
    // records add what the run is doing and its receipt, joined by prompt id.

    subscribeRunFeedback(listener) {
        if (typeof listener !== "function") return () => {};
        this._feedbackListeners.add(listener);
        // A read that landed before Generate mounted still reaches it.
        if (this._lastFeedbackRead) {
            try { listener({ read: this._lastFeedbackRead }); } catch (err) { console.warn("[Sonder] run feedback listener failed:", err); }
        }
        return () => this._feedbackListeners.delete(listener);
    }

    _publishFeedback(message) {
        for (const listener of [...this._feedbackListeners]) {
            try {
                listener(message);
            } catch (err) {
                console.warn("[Sonder] run feedback listener failed:", err);
            }
        }
    }

    applyRunProgress(event) {
        if (this._destroyed || !event?.record) return;
        this._publishFeedback({ record: event.record });
    }

    /** This session's run records, on load and after a reconnect. */
    async refreshRunFeedback() {
        if (!this.canWrite()) return;
        try {
            const result = await readResponse(await this._fetch(this._sessionPath("runs/feedback"), {
                headers: this.headers(false),
            }));
            if (result.ok) {
                this._lastFeedbackRead = result.payload;
                this._publishFeedback({ read: result.payload });
            }
        } catch (_err) {
            // The next reconnect reads again; the App's own status still ends its runs.
        }
    }

    async refreshStatus() {
        try {
            const result = await readResponse(await this._fetch(this._sessionPath("status"), {
                headers: this.headers(false),
            }));
            if (result.ok) this.applyStatus(result.payload, { fetched: true });
            else this._noteRefusal(result);
            return result;
        } catch (err) {
            return { ok: false, status: 0, payload: { code: "network", error: String(err?.message || err) } };
        }
    }

    // Resolves to { ok, status, payload }; never throws for HTTP refusals.
    // Generate saves the values it is sent before it can refuse a workflow
    // (`generate_refused`) or submit; any earlier refusal saved nothing, so the
    // pending edits are put back for the next save.
    async generate(count, { renderCacheMaxBytes = 0, signal = null, templateId: clicked = null } = {}) {
        const cancelEpoch = this._cancelEpoch;
        // The workflow this click meant: from the click itself when the toolbar
        // captured it (holdWorkflow), otherwise before anything below can await.
        const templateId = clicked ?? this.templateId;
        if (this._switching) {
            return { ok: false, status: 409, payload: { code: "workflow_switching", error: "Wait for the workflow switch to finish, then Generate." } };
        }
        this._setGenerating(1);
        try {
            return await this._generate(count, { renderCacheMaxBytes, signal, cancelEpoch, templateId });
        } finally {
            this._setGenerating(-1);
        }
    }

    // Run one output node and what it needs (Output Views); the same position,
    // Controls flush and ordering as Generate. Resolves like generate().
    async runBranch(target, { renderCacheMaxBytes = 0, signal = null, templateId: clicked = null } = {}) {
        const cancelEpoch = this._cancelEpoch;
        const templateId = clicked ?? this.templateId;
        if (this._switching) {
            return { ok: false, status: 409, payload: { code: "workflow_switching", error: "Wait for the workflow switch to finish." } };
        }
        this._setGenerating(1);
        try {
            return await this._generate(1, { renderCacheMaxBytes, signal, cancelEpoch, templateId, branchTarget: String(target ?? "") });
        } finally {
            this._setGenerating(-1);
        }
    }

    async _generate(count, { renderCacheMaxBytes, signal, cancelEpoch, templateId, branchTarget = null }) {
        if (this.flushControlsPanel && !await this.flushControlsPanel()) {
            return { ok: false, status: 409, payload: { code: "controls_not_saved", error: "A control edit was not saved." } };
        }
        // The panel decided above whether a save refuses this run; this only
        // keeps Generate from overtaking a Controls save still in flight.
        await this._controlsHost?.flush();
        const n = Math.max(1, Math.min(SDK_MAX_COUNT, Math.round(Number(count) || 1)));
        // An older save still in flight must not land after this one.
        await this.drainSaves();
        // Cancel can reach the server before Generate when client edits are
        // still draining. The click's signal also covers beforeGenerate in the
        // toolbar, before this method is entered. Do not submit a new epoch.
        if (signal?.aborted || cancelEpoch !== this._cancelEpoch) {
            void this.flushValues();
            await this.drainSaves();
            return { ok: false, status: 409, payload: { code: "canceled_before_submit" } };
        }
        const dirty = this._takeDirty();
        let result;
        try {
            result = await readResponse(await this._fetch(this._sessionPath(branchTarget === null ? "generate" : "branch"), {
                method: "POST",
                headers: this.headers(),
                body: JSON.stringify({
                    ...(branchTarget === null ? { count: n } : { target: branchTarget }),
                    editor_values: { ...this.values },
                    editor_values_seq: ++this._seq,
                    template_id: templateId,
                    // -1 is unlimited, 0 disables the cache (renderCacheMaxBytes).
                    render_cache_max_bytes: Math.max(-1, Math.round(Number(renderCacheMaxBytes) || 0)),
                }),
            }));
        } catch (err) {
            result = { ok: false, status: 0, payload: { code: "network", error: String(err?.message || err) } };
        }
        const saved = result.ok
            || result.payload.code === "generate_refused"
            || result.payload.code === "branch_not_output"
            || result.payload.code === "branch_refused"
            || result.payload.code === "prompt_no_outputs"
            || result.payload.code === "canceled_before_submit"
            || Array.isArray(result.payload.run_ids);
        // Retried shortly unless another tab owns the session, it has ended, or
        // ComfyUI is unreachable (the next edit, Take over or Exit sends it).
        const code = String(result.payload.code || "");
        if (!saved) this._restoreDirty(dirty, { schedule: !["not_owner", "session_ended", "network"].includes(code) });
        if (!result.ok) this._noteRefusal(result);
        await this._controlsHost?.refresh();
        return result;
    }

    // ── Workflow switching (Sonder App only) ────────────────────────────────

    // A Generate between its click and its response keeps the Workflow menu
    // disabled; the Controls host repaints the panel when this changes.
    get busy() {
        return this._generating > 0 || this._switching;
    }

    get switching() {
        return this._switching;
    }

    // From a Generate click until its answer: the toolbar awaits other work
    // before calling generate(), and a switch then would run the click on the
    // new workflow. Returns the release.
    holdWorkflow() {
        this._setGenerating(1);
        let held = true;
        return () => {
            if (held) this._setGenerating(-1);
            held = false;
        };
    }

    // The server's session runs another workflow than this page believed. Same
    // effect as a switch made here, without the queued-jobs count.
    _adoptWorkflow(templateId, name) {
        const previous = { template_id: this.templateId, name: this.templateName };
        this.templateId = templateId;
        this.templateName = name || this.templateName;
        this._replaceControlsHost();
        this._announceWorkflow({ template_id: this.templateId, name: this.templateName, previous, pending_jobs: 0,
            still_generating: this._stillGeneratingWith(), adopted: true });
    }

    _stillGeneratingWith() {
        return this.activeRuns().filter(run => run.template_id && run.template_id !== this.templateId).length;
    }

    _replaceControlsHost() {
        const retired = this._controlsHost;
        this._controlsHost = null;
        retired?.destroy();
    }

    _announceWorkflow(change) {
        for (const listener of [...this._workflowListeners]) {
            try {
                listener(change);
            } catch (err) {
                console.warn("[Sonder] workflow change listener failed:", err);
            }
        }
    }

    _setSwitching(value) {
        this._switching = value;
        this._controlsHost?.repaint?.();
        // The toolbar disables Generate while a switch is in flight.
        this._publish(this.status);
    }

    _setGenerating(delta) {
        this._generating = Math.max(0, this._generating + delta);
        this._controlsHost?.repaint?.();
    }

    onWorkflowChanged(listener) {
        if (typeof listener !== "function") return () => {};
        this._workflowListeners.add(listener);
        return () => this._workflowListeners.delete(listener);
    }

    async listWorkflows() {
        try {
            const result = await readResponse(await this._fetch(this._sessionPath("workflows"), { headers: this.headers(false) }));
            if (!result.ok) this._noteRefusal(result);
            return result;
        } catch (err) {
            return { ok: false, status: 0, payload: { code: "network", error: String(err?.message || err) } };
        }
    }

    async setFavorite(templateId, favorite) {
        try {
            return await readResponse(await this._fetch(this._apiURL("/sonder-editor/launcher/favorite"), {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ id: String(templateId || ""), favorite: !!favorite }),
            }));
        } catch (err) {
            return { ok: false, status: 0, payload: { code: "network", error: String(err?.message || err) } };
        }
    }

    // The panel has already saved or refused its edits. This waits for Controls
    // writes still queued (each carries the old workflow), then switches. A new
    // Controls host follows: its views and sequences belong to one workflow.
    async switchWorkflow(templateId) {
        if (!this.canWrite()) return { ok: false, status: 409, payload: { code: "not_owner", error: "This tab no longer owns the session." } };
        if (this.busy) return { ok: false, status: 409, payload: { code: "busy", error: "Wait for Generate to answer, then switch." } };
        this._setSwitching(true);
        // Refused or unanswered: the server may already run another workflow.
        let reread = true;
        try {
            await this._controlsHost?.flush();
            const previous = { template_id: this.templateId, name: this.templateName };
            let result;
            try {
                result = await readResponse(await this._fetch(this._sessionPath("workflow"), {
                    method: "POST",
                    headers: this.headers(),
                    body: JSON.stringify({ template_id: String(templateId || ""), expected_template_id: previous.template_id }),
                }));
            } catch (err) {
                return { ok: false, status: 0, payload: { code: "network", error: String(err?.message || err) } };
            }
            if (!result.ok) {
                this._noteRefusal(result);
                return result;
            }
            reread = false;
            this.templateId = String(result.payload.template_id || "");
            this.templateName = String(result.payload.template_name || "");
            this._replaceControlsHost();
            this._announceWorkflow({
                template_id: this.templateId,
                name: this.templateName,
                previous,
                pending_jobs: Number(result.payload.pending_jobs) || 0,
                // Takes from earlier workflows keep generating; they keep their own.
                still_generating: this._stillGeneratingWith(),
            });
            return result;
        } finally {
            this._setSwitching(false);
            if (reread) void this.refreshStatus();
        }
    }

    async cancelAll() {
        this._cancelEpoch++;
        try {
            const result = await readResponse(await this._fetch(this._sessionPath("cancel"), {
                method: "POST",
                headers: this.headers(),
                body: "{}",
            }));
            if (!result.ok) this._noteRefusal(result);
            return result;
        } catch (err) {
            return { ok: false, status: 0, payload: { code: "network", error: String(err?.message || err) } };
        }
    }

    destroy() {
        this._destroyed = true;
        this._controlsHost?.destroy();
        this.flushControlsPanel = null;
        if (this._flushTimer != null) this._clearTimeout(this._flushTimer);
        this._flushTimer = null;
        this._listeners.clear();
        this._feedbackListeners.clear();
        this._workflowListeners.clear();
        this.details.clear();
    }
}

// ── Session ownership ─────────────────────────────────────────────────────
// Who owns this SDK session in the session registry, kept in
// `host.ownerState`:
//   active / orphaned — this tab owns it (orphaned: a missed heartbeat)
//   reconnecting      — the server could not be reached; writes still try
//   displaced         — another tab claimed it; only Take over claims back
//   ended             — the server no longer knows it (restart, eviction)
// Every claim with the secret takes the session over, so a displaced tab must
// never claim on its own or two tabs would trade it back and forth. A
// released owner (orphan expiry, forced release) is claimed back quietly.
// `claim()` and `heartbeat()` resolve to the session client's payloads.
export function createSdkOwnership({
    host,
    tabSessionId,
    claim,
    heartbeat,
    onChange = () => {},
    onReclaimed = () => {},
    setIntervalImpl = (fn, ms) => globalThis.setInterval(fn, ms),
    clearIntervalImpl = (id) => globalThis.clearInterval(id),
    intervalMs = 10000,
}) {
    let timer = null;
    let reclaiming = null;
    let stopped = false;
    const passive = () => host.ownerState === "displaced" || host.ownerState === "ended";

    const stopHeartbeat = () => {
        if (timer != null) clearIntervalImpl(timer);
        timer = null;
    };
    const startHeartbeat = () => {
        stopHeartbeat();
        if (!stopped) timer = setIntervalImpl(beat, intervalMs);
    };
    const setState = (state) => {
        const previous = host.ownerState;
        host.setOwnerState(state);
        if (passive()) stopHeartbeat();
        onChange(state, previous);
    };

    function reclaim({ fromDisplaced = false } = {}) {
        if (reclaiming) return reclaiming;
        reclaiming = Promise.resolve().then(claim).then((result) => {
            if (stopped) return result;
            if (result?.ok) {
                setState(result.owner?.status || "active");
                startHeartbeat();
                onReclaimed();
            } else if (result?.code === "invalid_sdk_session" || result?.status === 409) {
                setState("ended");
            } else if (fromDisplaced) {
                setState("displaced");
            }
            return result;
        }).catch(() => {
            if (stopped) return null;
            if (fromDisplaced) {
                setState("displaced");
            } else {
                setState("reconnecting");
                if (timer == null) startHeartbeat();
            }
            return null;
        }).finally(() => {
            reclaiming = null;
        });
        return reclaiming;
    }

    function beat() {
        if (stopped || passive()) return Promise.resolve();
        return Promise.resolve().then(heartbeat).then((result) => {
            if (stopped || passive()) return;
            if (result?.ok && result.owner?.session_id === tabSessionId) {
                setState(result.owner.status || "active");
            } else if (result?.code === "locked" && result?.owner && result.owner.session_id !== tabSessionId) {
                setState("displaced");
            } else {
                return reclaim();
            }
        }).catch(() => {
            if (!stopped && !passive()) setState("reconnecting");
        });
    }

    return {
        start(initialStatus = "active") {
            stopped = false;
            setState(initialStatus || "active");
            startHeartbeat();
        },
        takeOver() {
            return reclaim({ fromDisplaced: host.ownerState === "displaced" });
        },
        // `session_changed` for this session's host id.
        handleSessionChanged(event) {
            if (stopped || passive()) return;
            const owner = event?.owner || null;
            if (owner?.session_id === tabSessionId) setState(owner.status || "active");
            else if (owner) setState("displaced");
            else void reclaim();
        },
        // A write was refused. `not_owner` covers both "another tab owns it"
        // and "nobody does", so ask the registry which before choosing.
        handleOwnershipLost(code) {
            if (stopped || host.ownerState === "ended") return Promise.resolve();
            if (code === "session_ended") {
                setState("ended");
                return Promise.resolve();
            }
            return beat();
        },
        beat,
        stop() {
            stopped = true;
            stopHeartbeat();
        },
        get heartbeatRunning() {
            return timer != null;
        },
    };
}
