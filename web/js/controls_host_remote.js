// Standalone host adapters; callers own identity and connection lifecycle.
import { AVAILABILITY_WRITE_DEBOUNCE_MS, createAvailabilityReader } from "./controls_availability_reader.js";
import { freshWithin } from "./controls_fresh_view.js";
import { NODE_PACKS_PATH, nodePackMappingsOnce } from "./node_pack_mappings.js";

const idle = () => ({ state: "idle", bindings: [], notes: [], error: null });
// The Sonder App has no graph to add a Relay to: Manage adds app-only Controls.
const SDK_EMPTY = "This workflow exposes no Controls. Choose Manage to add some for this app.";
const failure = (changes, reason) => ({
    ok: false,
    rejected: Object.keys(changes || {}).map(target => ({ target, reason })),
});
// No answer arrived, so the change may have landed: a caller reports it as
// not confirmed, never as not applied (controls_choice_apply.js).
const unconfirmed = (changes, reason) => ({ ...failure(changes, reason), unconfirmed: true });
const AVAILABILITY_TIMEOUT_MS = 15000;
// Why a mounted tab can't write: what `writeBlock` says, and the server's same answers.
const WRITE_REFUSALS = {
    canvas: "The canvas is not connected. Nothing was changed.",
    reconnecting: "This tab is reconnecting. Nothing was changed.",
    session: "This tab no longer owns the editor.",
};
const SERVER_REFUSALS = {
    canvas_host_disconnected: WRITE_REFUSALS.canvas,
    session_orphaned: WRITE_REFUSALS.reconnecting,
    not_owner: WRITE_REFUSALS.session,
};

export function createSdkControlsHost(owner) {
    let view = idle(),
        destroyed = false,
        visible = false,
        // A View pane or the pane picker shows: read on show, like the panel.
        viewsDemand = false,
        epoch = 0,
        seq = -1,
        read = null,
        reread = false,
        manage = { on: false, filter: "" },
        tail = Promise.resolve(),
        // Counts views adopted from the server, so freshView can tell its own read landed.
        adoptions = 0;
    const listeners = new Set();
    const emit = () => {
        for (const cb of listeners) cb(view);
    };
    const adopt = payload => {
        // A view of a workflow this page has switched away from.
        if (payload.template_id && owner.templateId && payload.template_id !== owner.templateId) return;
        if (Number(payload.seq) >= seq) {
            const moved = Number(payload.seq) > seq;
            seq = Number(payload.seq);
            view = { ...payload.view, state: "ready", error: payload.view?.error || null };
            adoptions++;
            emit();
            if (moved) availability.schedule(false, AVAILABILITY_WRITE_DEBOUNCE_MS);
        }
    };
    // Which Mode choices can't run here: only while the Modes table can show.
    const availability = createAvailabilityReader({
        active: () => visible && !manage.on,
        viewSeq: () => seq,
        templateId: () => owner.templateId,
        request: async fresh => {
            const response = await owner._fetch(owner._sessionPath(`availability${fresh ? "?fresh=1" : ""}`), {
                headers: owner.headers(false),
                // A dead connection must not hold the one read in flight.
                signal: globalThis.AbortSignal?.timeout?.(AVAILABILITY_TIMEOUT_MS),
            });
            if (response.ok) return response.json();
            // A session that ended says so here too: tabs return with nothing else to read.
            owner._noteRefusal?.({ ok: false, status: response.status, payload: await response.json().catch(() => ({})) });
            return null;
        },
        loadPacks: () => (owner._apiURL ? nodePackMappingsOnce(owner._apiURL(NODE_PACKS_PATH), owner._fetch) : null),
    });
    // Back at the tab: a file may have been installed meanwhile. An unusable
    // choice's state lives in the view, so that is read too when anything is lacking.
    const onReturn = () => {
        if (destroyed || !visible || manage.on || globalThis.document?.hidden) return;
        availability.schedule(true);
        const unusable = view.presets?.some(row => row.state === "unavailable" || row.state === "refused");
        if (availability.hasEntries() || unusable) void refresh();
    };
    globalThis.window?.addEventListener?.("focus", onReturn);
    globalThis.document?.addEventListener?.("visibilitychange", onReturn);
    // Manage's candidates are fetched only while it is open (amendment A1).
    const controlsPath = () =>
        owner._sessionPath("controls") +
        (manage.on ? `?manage=1&filter=${encodeURIComponent(manage.filter)}` : "");
    const request = async (method, changes, templateId) => {
        const response = await owner._fetch(controlsPath(), {
            method,
            headers: owner.headers(method !== "GET"),
            ...(changes ? { body: JSON.stringify({ changes, template_id: templateId }) } : {}),
        });
        const payload = await response.json();
        const result = { ok: response.ok, status: response.status, payload };
        if (!response.ok) owner._noteRefusal(result);
        return result;
    };
    async function refresh(force = false) {
        if (destroyed || (!visible && !viewsDemand && !force)) return view;
        if (read) {
            reread = true; // a Manage change during a read needs a fresh one
            return read;
        }
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
            if (reread && !destroyed) {
                reread = false;
                void refresh();
            }
        }
    }
    return {
        getView: () => view,
        availability,
        emptyMessage: SDK_EMPTY,
        // Sonder App curation (A1): hide the workflow's own items, remove only app-only ones.
        // serverRows: the server builds this prompt, so row lists edit without canvas row widgets.
        // exactIntegers: no canvas widget holds the value, so a seed keeps all 64 bits.
        capabilities: { hide: true, removeAuthored: false, serverRows: true, exactIntegers: true },
        setManage(on, filter = "") {
            const next = { on: !!on, filter: String(filter || "").slice(0, 120) };
            if (next.on === manage.on && next.filter === manage.filter) return;
            manage = next;
            epoch++;
            if (visible) {
                void refresh();
                availability.schedule();
            }
        },
        subscribe(cb) {
            listeners.add(cb);
            return () => listeners.delete(cb);
        },
        setVisible(value) {
            visible = !!value;
            if (visible) {
                void refresh();
                availability.schedule();
            }
        },
        setViewsDemand(value) {
            if (viewsDemand === !!value) return;
            viewsDemand = !!value;
            if (viewsDemand) void refresh();
        },
        refresh,
        // The session's Controls read after this call: a read already running
        // started before it, so it is awaited first and never counts.
        freshView(options) {
            return freshWithin(() => view, async () => {
                if (read) await read;
                const before = adoptions;
                const next = await refresh(true);
                return adoptions > before ? next : null;
            }, options);
        },
        applyChanges(changes) {
            const owned = { ...changes };
            // The workflow this edit was made on, whatever happens while it queues.
            const templateId = owner.templateId;
            if (owner.switching) return Promise.resolve(failure(owned, "The workflow is switching. Make this change after it."));
            const work = tail.then(async () => {
                if (destroyed || !owner.canWrite()) return failure(owned, "This session no longer owns the editor.");
                epoch++;
                try {
                    const result = await request("PUT", owned, templateId);
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
                    return unconfirmed(owned, "The change could not be confirmed.");
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
        // The Sonder App's Workflow menu (controls_workflow_header.js renders it).
        workflows: {
            current: () => ({ template_id: owner.templateId, name: owner.templateName }),
            busy: () => !!owner.busy,
            list: () => owner.listWorkflows(),
            switchTo: templateId => owner.switchWorkflow(templateId),
            setFavorite: (templateId, favorite) => owner.setFavorite(templateId, favorite),
        },
        // Repaint with the same view (Generate started or answered).
        repaint() {
            if (!destroyed) emit();
        },
        destroy() {
            destroyed = true;
            epoch++;
            listeners.clear();
            availability.destroy();
            globalThis.window?.removeEventListener?.("focus", onReturn);
            globalThis.document?.removeEventListener?.("visibilitychange", onReturn);
        },
    };
}

export function createMountedControlsHost({
    projectId,
    hostId,
    sessionId,
    apiURL,
    fetchImpl = (...args) => fetch(...args),
    // "" when this tab may write, else a key of WRITE_REFUSALS.
    writeBlock = () => "",
}) {
    let view = idle(),
        // The server says no canvas holds this host. Presence decides, not a read.
        canvasGone = false,
        visible = false,
        // A View pane or the pane picker shows here: the canvas keeps describing.
        viewsDemand = false,
        manage = { on: false, filter: "" },
        destroyed = false,
        seq = -1,
        hostSession = "",
        read = null,
        readStamp = -1,
        epoch = 0,
        posting = 0,
        reporting = null,
        reportRequested = false,
        postTail = Promise.resolve(),
        // Counts publications adopted, so freshView can tell its own read landed.
        adoptions = 0;
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
        for (const pending of waiting.values()) pending.resolve(unconfirmed(pending.changes, reason));
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
        if (destroyed || writeBlock()) return;
        reportRequested = true;
        if (reporting) return reporting;
        reporting = (async () => {
            while (reportRequested && !destroyed && !writeBlock()) {
                reportRequested = false;
                const receipts = [...seen].slice(0, 128),
                    sentVisible = visible;
                try {
                    const result = await mutation("controls_visibility", {
                        visible: sentVisible,
                        seen: receipts,
                        manage: manage.on,
                        filter: manage.filter,
                        views: viewsDemand,
                    });
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
        if (destroyed || (!visible && !viewsDemand && !waiting.size && !force)) return view;
        // A read from before the last epoch change will throw its answer away.
        if (read && readStamp !== epoch) return read.then(() => refresh(force));
        if (read) return read;
        const stamp = epoch;
        readStamp = stamp;
        read = (async () => {
            try {
                const response = await fetchImpl(path("controls") + `?host_id=${encodeURIComponent(hostId)}`);
                const payload = await response.json();
                if (destroyed || stamp !== epoch) return view;
                if (!response.ok) throw Error("Controls could not be loaded.");
                if (payload.view && canvasGone) {
                    // Not adopted: presence says the canvas left, and it reads again
                    // when presence says it's back. Receipts below still count.
                } else if (payload.view) {
                    // Sequences only grow within one relay; a lower one means the
                    // relay was cleared and recreated between two reads.
                    if (seq > 0 && payload.view_seq < seq) {
                        abandon("The canvas reconnected before it confirmed this change.");
                    }
                    seq = payload.view_seq;
                    // The canvas publishes an error view when it cannot send its controls.
                    const error = payload.view.error || null;
                    view = { ...payload.view, state: error ? "error" : "ready", error };
                    adoptions++;
                    emit();
                } else {
                    seq = 0;
                    view = { ...idle(), state: canvasGone ? "disconnected" : "loading" };
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
        setViewsDemand(value) {
            if (viewsDemand === !!value) return;
            viewsDemand = !!value;
            void report();
            if (viewsDemand) void refresh(true);
        },
        // The canvas lists candidates, filtered, while Manage is open here.
        capabilities: { hide: false, removeAuthored: true },
        setManage(on, filter = "") {
            const next = { on: !!on, filter: String(filter || "").slice(0, 120) };
            if (next.on === manage.on && next.filter === manage.filter) return;
            manage = next;
            void report();
        },
        refresh,
        // The canvas's publication read after this call. A publication the
        // canvas marked `view_stale` (its describe after a write was not
        // adopted) is not fresh: it may predate the write it acknowledges.
        freshView(options) {
            return freshWithin(() => view, async () => {
                if (read) await read;
                const before = adoptions;
                const next = await refresh(true);
                return adoptions > before ? next : null;
            }, options);
        },
        presence(payload = {}) {
            if (payload.host_id && payload.host_id !== hostId) return;
            let reread = false;
            if (payload.canvas_host_connected === false && !canvasGone) {
                canvasGone = true;
                epoch++;  // a read already running must not repaint what the canvas had
                view = { ...idle(), state: "disconnected" };
                emit();
            } else if (payload.canvas_host_connected === true && canvasGone) {
                canvasGone = false;
                epoch++;
                view = { ...idle(), state: "loading" };
                emit();
                reread = true;
            }
            const next = String(payload.host?.session_tag || "");
            if (next && next !== hostSession) {
                // The first observed session only establishes identity; a later
                // one is a canvas restart whose relay starts empty.
                if (hostSession) abandon("The canvas restarted before it confirmed this change.");
                hostSession = next;
                epoch++;
                seq = -1;
                void report();
                reread = true;
            }
            // One read, after both: a canvas reload brings both in one payload.
            if (reread) void refresh(true);
            if (payload.controls_visible !== undefined && payload.controls_visible !== visible) void report();
            if (payload.controls_views !== undefined && payload.controls_views !== viewsDemand) void report();
            if (
                payload.controls_view_seq !== undefined &&
                payload.controls_view_seq !== seq &&
                (visible || viewsDemand || waiting.size)
            ) {
                void refresh();
            }
        },
        changed: event => {
            if (!event.host_id || event.host_id === hostId) void refresh();
        },
        applyChanges(changes) {
            const owned = { ...changes };
            const refusal = () => (destroyed ? "The editor has closed." : WRITE_REFUSALS[writeBlock()] || "");
            if (refusal()) return Promise.resolve(failure(owned, refusal()));
            posting++;
            return new Promise(resolve => {
                const post = postTail.then(async () => {
                    if (refusal()) {
                        resolve(failure(owned, refusal()));
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
                                failure(owned, SERVER_REFUSALS[result.payload.code] || "The canvas could not accept the change.").rejected,
                        });
                        return;
                    }
                    waiting.set(result.payload.request_id, { resolve, changes: owned });
                    void refresh(true);
                });
                postTail = post
                    .catch(() => resolve(unconfirmed(owned, "The change could not be confirmed.")))
                    .finally(() => {
                        posting--;
                        if (waiting.size) void refresh(true);
                    });
            });
        },
        // A run this tab asks for must not overtake its own Controls edits: wait
        // until every posted change is sent and the canvas answered it. False
        // when some are still unanswered at the deadline.
        async flush({ timeoutMs = 10000 } = {}) {
            const deadline = Date.now() + timeoutMs;
            await postTail;
            while (waiting.size && !destroyed && Date.now() < deadline) {
                await new Promise(resolve => setTimeout(resolve, 100));
            }
            return !waiting.size;
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
