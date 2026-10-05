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
        this.values = { ...(defaults || {}), ...(initialValues || {}) };
        this.ownerState = "active";
        this.status = { seq: -1, runs: [] };
        this.onOwnershipLost = null;
        this._fetch = fetchImpl;
        this._apiURL = apiURL;
        this._setTimeout = setTimeoutImpl;
        this._clearTimeout = clearTimeoutImpl;
        this._debounceMs = debounceMs;
        this._dirty = {};
        this._flushTimer = null;
        this._inflightFlush = null;
        this._listeners = new Set();
        this._destroyed = false;
    }

    // ── EditorWidget host contract ─────────────────────────────────────────

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
                    body: JSON.stringify({ values: { ...this.values } }),
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
        this._inflightFlush = request;
        try {
            return await request;
        } finally {
            if (this._inflightFlush === request) this._inflightFlush = null;
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
        if (fetched ? seq < this.status.seq : seq <= this.status.seq) return false;
        if (this.ownerState === "ended") return false;
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
    async generate(count, { renderCacheMaxBytes = 0 } = {}) {
        const n = Math.max(1, Math.min(SDK_MAX_COUNT, Math.round(Number(count) || 1)));
        // An older save still in flight must not land after this one.
        if (this._inflightFlush) await this._inflightFlush.catch(() => {});
        const dirty = this._takeDirty();
        let result;
        try {
            result = await readResponse(await this._fetch(this._sessionPath("generate"), {
                method: "POST",
                headers: this.headers(),
                body: JSON.stringify({
                    count: n,
                    editor_values: { ...this.values },
                    // -1 is unlimited, 0 disables the cache (renderCacheMaxBytes).
                    render_cache_max_bytes: Math.max(-1, Math.round(Number(renderCacheMaxBytes) || 0)),
                }),
            }));
        } catch (err) {
            result = { ok: false, status: 0, payload: { code: "network", error: String(err?.message || err) } };
        }
        const saved = result.ok
            || result.payload.code === "generate_refused"
            || Array.isArray(result.payload.run_ids);
        // Retried shortly unless another tab owns the session, it has ended, or
        // ComfyUI is unreachable (the next edit, Take over or Exit sends it).
        const code = String(result.payload.code || "");
        if (!saved) this._restoreDirty(dirty, { schedule: !["not_owner", "session_ended", "network"].includes(code) });
        if (!result.ok) this._noteRefusal(result);
        return result;
    }

    async cancelAll() {
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
        if (this._flushTimer != null) this._clearTimeout(this._flushTimer);
        this._flushTimer = null;
        this._listeners.clear();
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
