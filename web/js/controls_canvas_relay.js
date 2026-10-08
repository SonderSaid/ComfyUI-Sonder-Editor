// Canvas half of the mounted Controls transport. It owns requests and retries;
// the injected canvas adapter remains the only graph writer.

// session_registry.CONTROLS_MAX_BYTES; one publication must fit in a request.
const MAX_PUBLICATION_BYTES = 512 * 1024 - 1024;
const OPTION_PREVIEW = 200;
// Note previews shorten in steps; each step keeps every control usable.
const NOTE_PREVIEWS = [500, 80];
// The registry refuses these for good; resending the same publication cannot help.
const RETIRED_CODES = ["stale_view", "invalid_ack", "host_changed"];
const UNSENDABLE_CODES = ["too_large", "invalid_view"];
const unavailableView = error => Object.freeze({ bindings: [], notes: [], error });
const TOO_LARGE_VIEW = unavailableView(
    "This workflow's Controls are too large to show in a mounted tab. Edit them on the canvas.",
);
const INVALID_VIEW = unavailableView(
    "This workflow's Controls could not be shown in a mounted tab. Edit them on the canvas.",
);

const publicationBytes = value => new TextEncoder().encode(JSON.stringify(value)).length;
const shorten = (descriptor, field, limit) =>
    typeof descriptor[field] === "string" && descriptor[field].length > limit
        ? { ...descriptor, [field]: descriptor[field].slice(0, limit), note_truncated: true }
        : descriptor;

// Shrink display-only parts until the publication fits. A capped option preview
// and a truncated note keep their existing rules: the canvas writer validates
// full option membership, and a truncated note is edited on the canvas.
export function fitPublicationView(view, envelope) {
    // Server-owned labels only help the canvas filter its candidate inventory.
    // They are not part of a mounted tab's display or transport budget.
    if (view.candidate_labels) {
        view = { ...view };
        delete view.candidate_labels;
    }
    const fits = candidate => publicationBytes({ ...envelope, view: candidate }) <= MAX_PUBLICATION_BYTES;
    if (fits(view)) return view;
    // Manage's candidates go first: a narrower filter shows the rest.
    while (view.candidates?.length && !fits(view)) {
        view = { ...view, candidates: view.candidates.slice(0, Math.floor(view.candidates.length / 2)), candidates_truncated: true };
    }
    if (fits(view)) return view;
    // Unlisted anchors are for Manage only; they go next.
    view = { ...view, suggestions: [] };
    if (fits(view)) return view;
    const capped = {
        ...view,
        bindings: (view.bindings || []).map(d =>
            Array.isArray(d.options) && d.options.length > OPTION_PREVIEW
                ? { ...d, options: d.options.slice(0, OPTION_PREVIEW), options_truncated: true }
                : d,
        ),
    };
    if (fits(capped)) return capped;
    for (const limit of NOTE_PREVIEWS) {
        const trimmed = {
            ...capped,
            bindings: capped.bindings.map(d => shorten(d, "note", limit)),
            notes: (capped.notes || []).map(d => shorten(d, "text", limit)),
        };
        if (fits(trimmed)) return trimmed;
    }
    return TOO_LARGE_VIEW;
}

export function createCanvasControlsRelay({ host, identity, api, connected = () => false }) {
    let destroyed = false,
        epoch = 0,
        active = null,
        trailing = false,
        publication = null,
        lastHash = null,
        mountedVisible = false;
    const processed = new Map(),
        allowedHashes = new Set();
    const path = suffix => {
        const id = identity();
        return `/sonder-editor/project/${encodeURIComponent(id.projectId)}/${suffix}`;
    };
    const current = (stamp, id) => !destroyed && epoch === stamp && JSON.stringify(identity()) === JSON.stringify(id);
    const request = async (method, suffix, body) => {
        const id = identity();
        const query =
            method === "GET"
                ? `?host_id=${encodeURIComponent(id.hostId)}&session_id=${encodeURIComponent(id.sessionId)}`
                : "";
        const response = await api.fetchApi(path(suffix) + query, {
            method,
            headers: { "Content-Type": "application/json" },
            ...(body ? { body: JSON.stringify(body) } : {}),
        });
        let payload;
        try {
            payload = await response.json();
        } catch (_) {
            // aiohttp answers an oversized body with plain text, not JSON.
            payload = { code: response.status === 413 ? "too_large" : "invalid_response" };
        }
        return { ok: response.ok, payload };
    };
    // The view could not be stored as sent. Keep its acknowledgements moving
    // with a view that tells the tab why its controls are unavailable.
    async function sendUnavailable(stamp, id, code) {
        publication = { ...publication, view: code === "too_large" ? TOO_LARGE_VIEW : INVALID_VIEW };
        const result = await request("PUT", "controls", publication);
        if (!current(stamp, id)) return null;
        if (!result.ok && [...RETIRED_CODES, ...UNSENDABLE_CODES].includes(result.payload.code)) publication = null;
        return result;
    }
    async function publish(stamp, id, seq) {
        if (publication) {
            let retry = await request("PUT", "controls", publication);
            if (!current(stamp, id)) return false;
            if (!retry.ok && UNSENDABLE_CODES.includes(retry.payload.code)) {
                retry = await sendUnavailable(stamp, id, retry.payload.code);
                if (!retry) return false;
            }
            if (!retry.ok) {
                if (RETIRED_CODES.includes(retry.payload.code)) publication = null;
                return false;
            }
            for (const rid of publication.applied) processed.delete(rid);
            for (const row of publication.rejected) processed.delete(row.request_id);
            seq = publication.view_seq;
            publication = null;
        }
        const view = host.getView();
        if (view.state !== "ready") return true;
        const hash = await host.getPromptHash();
        if (!current(stamp, id)) return false;
        if (hash !== lastHash) {
            allowedHashes.clear();
            allowedHashes.add(hash);
        }
        if (hash === lastHash && !processed.size && seq > 0) return true;
        const applied = [],
            rejected = [];
        for (const [rid, outcome] of processed) {
            if (outcome.ok) applied.push(rid);
            else for (const row of outcome.rejected) rejected.push({ request_id: rid, ...row });
        }
        const envelope = {
            host_id: id.hostId,
            session_id: id.sessionId,
            view_seq: Math.max(seq, 0) + 1,
            prompt_hash: hash,
            applied,
            rejected,
        };
        publication = { ...envelope, view: fitPublicationView(view, envelope) };
        let result = await request("PUT", "controls", publication);
        if (!current(stamp, id)) return false;
        if (!result.ok && UNSENDABLE_CODES.includes(result.payload.code)) {
            result = await sendUnavailable(stamp, id, result.payload.code);
            if (!result) return false;
        }
        if (result.ok) {
            lastHash = hash;
            for (const rid of applied) processed.delete(rid);
            for (const row of rejected) processed.delete(row.request_id);
            publication = null;
            return true;
        }
        if (RETIRED_CODES.includes(result.payload.code)) publication = null;
        return false;
    }
    async function cycle() {
        if (destroyed) return;
        if (active) {
            trailing = true;
            return active;
        }
        const id = identity(),
            stamp = epoch;
        if (!id.projectId || !id.hostId || !id.sessionId) return;
        active = (async () => {
            try {
                const queue = await request("GET", "control_changes");
                if (!queue.ok || !current(stamp, id)) return;
                mountedVisible = !!queue.payload.controls_visible;
                host.setMountedManage?.(!!queue.payload.controls_manage, queue.payload.controls_filter || "");
                host.setMountedVisible(mountedVisible);
                // The GET preceded this retry. Once its ACK succeeds that pending
                // snapshot is stale; fetch again before considering any writes.
                if (publication) {
                    if (await publish(stamp, id, queue.payload.controls_view_seq)) trailing = true;
                    return;
                }
                const liveRequests = new Set((queue.payload.pending || []).map(r => r.request_id));
                for (const rid of processed.keys()) if (!publication && !liveRequests.has(rid)) processed.delete(rid);
                if (queue.payload.controls_visible) await host.refresh();
                for (const pending of queue.payload.pending || []) {
                    if (!current(stamp, id)) return;
                    if (processed.has(pending.request_id)) continue;
                    const hash = await host.getPromptHash();
                    if (!current(stamp, id)) return;
                    if (hash !== lastHash) {
                        allowedHashes.clear();
                        allowedHashes.add(hash);
                        lastHash = hash;
                    }
                    if (!allowedHashes.has(pending.prompt_hash) && pending.prompt_hash !== hash) {
                        processed.set(pending.request_id, {
                            ok: false,
                            rejected: Object.keys(pending.changes).map(target => ({
                                target,
                                reason: "The workflow changed before this edit reached the canvas. Try again.",
                            })),
                        });
                        continue;
                    }
                    allowedHashes.add(pending.prompt_hash);
                    const outcome = await host.applyChanges(pending.changes, {
                        expectedPromptHash: hash,
                        canApply: async () => {
                            if (!current(stamp, id)) return false;
                            // Registry membership, rather than a delayed owner observation,
                            // decides whether the accepted intent still has permission.
                            const latest = await request("GET", "control_changes");
                            return (
                                latest.ok &&
                                current(stamp, id) &&
                                (latest.payload.pending || []).some(
                                    row =>
                                        row.request_id === pending.request_id && row.session_id === pending.session_id,
                                )
                            );
                        },
                    });
                    if (!current(stamp, id)) return;
                    processed.set(pending.request_id, outcome);
                    if (outcome.ok) {
                        lastHash = await host.getPromptHash();
                        allowedHashes.add(lastHash);
                    }
                }
                if (processed.size) await host.refresh(true);
                if (current(stamp, id) && (queue.payload.controls_visible || processed.size))
                    await publish(stamp, id, queue.payload.controls_view_seq);
            } catch (_) {
                /* State remains for event/poll retry; no graph write is replayed. */
            }
        })();
        try {
            return await active;
        } finally {
            active = null;
            if (trailing && !destroyed) {
                trailing = false;
                queueMicrotask(() => void cycle());
            }
        }
    }
    const unsubscribe = host.subscribe(view => {
        if (view.state === "ready") void cycle();
    });
    const timer = setInterval(() => {
        if (!connected() || publication || (mountedVisible && host.getView().state === "error")) void cycle();
    }, 2000);
    return {
        cycle,
        destroy() {
            destroyed = true;
            epoch++;
            clearInterval(timer);
            unsubscribe();
            host.setMountedManage?.(false, "");
            host.setMountedVisible(false);
            processed.clear();
        },
    };
}
