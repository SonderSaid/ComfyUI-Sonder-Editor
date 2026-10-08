// Standalone host adapters; callers own identity and connection lifecycle.
const idle = () => ({ state: "idle", bindings: [], notes: [], error: null });
// The Sonder App has no graph to add a Relay to; the workflow decides.
const SDK_EMPTY = "This workflow exposes no Controls.";
const failure = (changes, reason) => ({
    ok: false,
    rejected: Object.keys(changes || {}).map(target => ({ target, reason })),
});

export function createSdkControlsHost(owner) {
    let view = idle(),
        destroyed = false,
        visible = false,
        epoch = 0,
        seq = -1,
        read = null,
        tail = Promise.resolve();
    const listeners = new Set();
    const emit = () => {
        for (const cb of listeners) cb(view);
    };
    const adopt = payload => {
        if (Number(payload.seq) >= seq) {
            seq = Number(payload.seq);
            view = { ...payload.view, state: "ready", error: payload.view?.error || null };
            emit();
        }
    };
    const request = async (method, changes) => {
        const response = await owner._fetch(owner._sessionPath("controls"), {
            method,
            headers: owner.headers(method !== "GET"),
            ...(changes ? { body: JSON.stringify({ changes }) } : {}),
        });
        const payload = await response.json();
        const result = { ok: response.ok, status: response.status, payload };
        if (!response.ok) owner._noteRefusal(result);
        return result;
    };
    async function refresh() {
        if (destroyed || !visible) return view;
        if (read) return read;
        const stamp = epoch;
        read = (async () => {
            try {
                const result = await request("GET");
                if (!destroyed && stamp === epoch) {
                    if (result.ok) adopt(result.payload);
                    else {
                        view = { ...view, state: "error", error: "Controls could not be loaded." };
                        emit();
                    }
                }
            } catch (_) {
                if (!destroyed && stamp === epoch) {
                    view = { ...view, state: "error", error: "Controls could not be loaded." };
                    emit();
                }
            }
            return view;
        })();
        try {
            return await read;
        } finally {
            read = null;
        }
    }
    return {
        getView: () => view,
        emptyMessage: SDK_EMPTY,
        subscribe(cb) {
            listeners.add(cb);
            return () => listeners.delete(cb);
        },
        setVisible(value) {
            visible = !!value;
            if (visible) void refresh();
        },
        refresh,
        applyChanges(changes) {
            const owned = { ...changes };
            const work = tail.then(async () => {
                if (destroyed || !owner.canWrite()) return failure(owned, "This session no longer owns the editor.");
                epoch++;
                try {
                    const result = await request("PUT", owned);
                    if (destroyed) return failure(owned, "The editor has closed.");
                    if (result.ok) {
                        adopt(result.payload);
                        return { ok: true, rejected: [] };
                    }
                    return {
                        ok: false,
                        rejected: result.payload.rejected || failure(owned, "The change could not be saved.").rejected,
                    };
                } catch (_) {
                    return failure(owned, "The change could not be confirmed.");
                }
            });
            tail = work.catch(() => {});
            return work;
        },
        // Generate must not overtake a save. Which outcomes refuse a run is the
        // panel's decision (flushPending); an old refusal here must not linger.
        async flush() {
            await tail;
        },
        destroy() {
            destroyed = true;
            epoch++;
            listeners.clear();
        },
    };
}

export function createMountedControlsHost({
    projectId,
    hostId,
    sessionId,
    apiURL,
    fetchImpl = (...args) => fetch(...args),
    canWrite = () => true,
}) {
    let view = idle(),
        visible = false,
        destroyed = false,
        seq = -1,
        hostSession = "",
        read = null,
        epoch = 0,
        posting = 0,
        reporting = null,
        reportRequested = false,
        postTail = Promise.resolve();
    const listeners = new Set(),
        waiting = new Map(),
        seen = new Set();
    const path = suffix => apiURL(`/sonder-editor/project/${encodeURIComponent(projectId)}/${suffix}`);
    const emit = () => {
        for (const cb of listeners) cb(view);
    };
    // The server dropped this host's relay (release, eviction or a new canvas
    // session), so no acknowledgement can arrive for requests still waiting.
    const abandon = reason => {
        for (const pending of waiting.values()) pending.resolve(failure(pending.changes, reason));
        waiting.clear();
    };
    const mutation = async (suffix, body) => {
        const response = await fetchImpl(path(suffix), {
            method: suffix === "control_changes" ? "POST" : "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ host_id: hostId, session_id: sessionId, ...body }),
        });
        return { ok: response.ok, payload: await response.json() };
    };
    async function report() {
        if (destroyed || !canWrite()) return;
        reportRequested = true;
        if (reporting) return reporting;
        reporting = (async () => {
            while (reportRequested && !destroyed && canWrite()) {
                reportRequested = false;
                const receipts = [...seen].slice(0, 128),
                    sentVisible = visible;
                try {
                    const result = await mutation("controls_visibility", { visible: sentVisible, seen: receipts });
                    if (!result.ok) {
                        reportRequested = true;
                        break;
                    }
                    for (const rid of receipts) seen.delete(rid);
                } catch (_) {
                    reportRequested = true;
                    break;
                }
            }
        })();
        try {
            return await reporting;
        } finally {
            reporting = null;
        }
    }
    async function refresh(force = false) {
        if (destroyed || (!visible && !waiting.size && !force)) return view;
        if (read) return read;
        const stamp = epoch;
        read = (async () => {
            try {
                const response = await fetchImpl(path("controls") + `?host_id=${encodeURIComponent(hostId)}`);
                const payload = await response.json();
                if (destroyed || stamp !== epoch) return view;
                if (!response.ok) throw Error("Controls could not be loaded.");
                if (payload.view) {
                    // Sequences only grow within one relay; a lower one means the
                    // relay was cleared and recreated between two reads.
                    if (seq > 0 && payload.view_seq < seq) {
                        abandon("The canvas reconnected before it confirmed this change.");
                    }
                    seq = payload.view_seq;
                    // The canvas publishes an error view when it cannot send its controls.
                    const error = payload.view.error || null;
                    view = { ...payload.view, state: error ? "error" : "ready", error };
                    emit();
                } else {
                    seq = 0;
                    view = { ...idle(), state: "loading" };
                    emit();
                    abandon("The canvas closed before it confirmed this change.");
                }
                const refusals = new Map();
                for (const row of payload.rejected || []) {
                    if (!refusals.has(row.request_id)) refusals.set(row.request_id, []);
                    refusals.get(row.request_id).push({ target: row.target, reason: row.reason });
                }
                for (const rid of [...(payload.applied || []), ...refusals.keys()]) {
                    const pending = waiting.get(rid);
                    if (pending) {
                        waiting.delete(rid);
                        pending.resolve({ ok: !refusals.has(rid), rejected: refusals.get(rid) || [] });
                    }
                    if (pending || !posting) seen.add(rid);
                }
                void report();
            } catch (error) {
                if (!destroyed && stamp === epoch) {
                    view = { ...view, state: "error", error: error.message };
                    emit();
                }
            }
            return view;
        })();
        try {
            return await read;
        } finally {
            read = null;
        }
    }
    const timer = setInterval(() => {
        if (waiting.size) void refresh();
        if (seen.size || reportRequested) void report();
    }, 2000);
    return {
        getView: () => view,
        subscribe(cb) {
            listeners.add(cb);
            return () => listeners.delete(cb);
        },
        setVisible(value) {
            visible = !!value;
            void report();
            if (visible) void refresh(true);
        },
        refresh,
        presence(payload = {}) {
            if (payload.host_id && payload.host_id !== hostId) return;
            const next = String(payload.host?.session_id || "");
            if (next && next !== hostSession) {
                // The first observed session only establishes identity; a later
                // one is a canvas restart whose relay starts empty.
                if (hostSession) abandon("The canvas restarted before it confirmed this change.");
                hostSession = next;
                epoch++;
                seq = -1;
                void report();
                void refresh(true);
            }
            if (payload.controls_visible !== undefined && payload.controls_visible !== visible) void report();
            if (
                payload.controls_view_seq !== undefined &&
                payload.controls_view_seq !== seq &&
                (visible || waiting.size)
            ) {
                void refresh();
            }
        },
        changed: event => {
            if (!event.host_id || event.host_id === hostId) void refresh();
        },
        applyChanges(changes) {
            const owned = { ...changes };
            if (destroyed || !canWrite()) return Promise.resolve(failure(owned, "This tab no longer owns the editor."));
            posting++;
            return new Promise(resolve => {
                const post = postTail.then(async () => {
                    if (destroyed || !canWrite()) {
                        resolve(failure(owned, "This tab no longer owns the editor."));
                        return;
                    }
                    const result = await mutation("control_changes", { changes: owned });
                    if (destroyed) {
                        resolve(failure(owned, "The editor has closed."));
                        return;
                    }
                    if (!result.ok) {
                        resolve({
                            ok: false,
                            rejected:
                                result.payload.rejected ||
                                failure(owned, "The canvas could not accept the change.").rejected,
                        });
                        return;
                    }
                    waiting.set(result.payload.request_id, { resolve, changes: owned });
                    void refresh(true);
                });
                postTail = post
                    .catch(() => resolve(failure(owned, "The change could not be confirmed.")))
                    .finally(() => {
                        posting--;
                        if (waiting.size) void refresh(true);
                    });
            });
        },
        destroy() {
            destroyed = true;
            epoch++;
            clearInterval(timer);
            for (const pending of waiting.values()) pending.resolve(failure(pending.changes, "The editor has closed."));
            waiting.clear();
            listeners.clear();
        },
    };
}
