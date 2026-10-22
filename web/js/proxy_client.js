// Proxy media requests (`server/routes.py`, Proxy media). None of them reads or
// writes project.json: they touch the derived proxy cache and the server's build
// queue, so they carry no version precondition and no change certificate (an
// ungated read, see architecture.md, change certificates).
//
// Every call is best effort and never throws: a lost request costs a later
// reconcile, never a broken surface.

import { proxyStore } from "./media_display_source.js";
import { classifyMediaFailure } from "./media_preview_support.js";

const RECONCILE_INTERVAL_MS = 5_000;
const DEMAND_INTERVAL_MS = 30_000;
const REFRESH_INTERVAL_MS = 2_000;

const lastReconcile = new Map();       // projectId -> ms
const lastDemand = new Map();          // `${projectId}|${assetId}` -> ms
const reportedFailures = new Set();    // `${projectId}|${assetId}|${variant}|${rev}`
const refreshes = new Map();           // projectId -> { promise, at }

function apiUrl(path) {
    const api = window.comfyAPI?.api?.api;
    return api?.apiURL ? api.apiURL(path) : path;
}

function projectPath(projectId, suffix) {
    return `/sonder-editor/project/${encodeURIComponent(String(projectId || ""))}${suffix}`;
}

/**
 * POST a proxy request: `{ ok, status, payload, error }`, never a throw. `error` is
 * the server's code (`proxy_not_current`, `project_not_found`, …) or "network".
 */
export async function postProxyRequest(projectId, suffix, body = {}) {
    try {
        const response = await fetch(apiUrl(projectPath(projectId, suffix)), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        let payload = null;
        try { payload = await response.json(); } catch (_) {}
        return {
            ok: !!response.ok,
            status: response.status,
            payload,
            error: response.ok ? "" : String(payload?.code || payload?.error_code || `http_${response.status}`),
        };
    } catch (_error) {
        return { ok: false, status: 0, payload: null, error: "network" };
    }
}

async function postJson(projectId, suffix, body = {}) {
    const result = await postProxyRequest(projectId, suffix, body);
    return result.ok ? result.payload : null;
}

/** The URL of an asset's proxy at `rev`. Immutable: a rebuild gets a new rev. */
export function proxyFileUrl(projectId, assetId, rev) {
    if (!projectId || !assetId || !rev) return null;
    return apiUrl(projectPath(projectId,
        `/proxy/${encodeURIComponent(String(assetId))}/${encodeURIComponent(String(rev))}.mp4`));
}

/**
 * Read the authoritative proxy state (after a reconnect, say) into the store, and
 * return `GET /proxies`. Every socket of a project reconnects at once after a
 * restart: one read serves them all.
 */
export function refreshProxyStates(projectId, { now = Date.now(), force = false } = {}) {
    if (!projectId) return Promise.resolve(null);
    const recent = refreshes.get(projectId);
    // `force`: a read that must reflect a change just made (a settings save), so
    // neither a read in flight nor a recent one can answer it.
    if (!force && recent && (recent.pending || now - recent.at < REFRESH_INTERVAL_MS)) return recent.promise;
    const record = { at: now, pending: true, promise: null };
    record.promise = (async () => {
        try {
            const response = await fetch(apiUrl(projectPath(projectId, "/proxies")));
            if (!response.ok) return null;
            const payload = await response.json();
            proxyStore.ingestOverview(projectId, payload);
            proxyStore.ingestQueue(projectId, payload?.queue, payload?.epoch);
            return payload;
        } catch (_error) {
            return null;
        } finally {
            record.pending = false;
        }
    })();
    refreshes.set(projectId, record);
    return record.promise;
}

/** Queue stale or missing decisions and sweep orphans. At most every few seconds. */
export function requestProxyReconcile(projectId, { now = Date.now() } = {}) {
    if (!projectId) return false;
    if (now - (lastReconcile.get(projectId) || 0) < RECONCILE_INTERVAL_MS) return false;
    lastReconcile.set(projectId, now);
    void postJson(projectId, "/proxies/reconcile");
    return true;
}

/** Blocked clips on screen: build these first. Each asset at most every 30 s. */
export function reportProxyDemand(projectId, assetIds = [], { now = Date.now() } = {}) {
    if (!projectId) return [];
    const due = [];
    for (const assetId of assetIds) {
        const id = String(assetId || "");
        const key = `${projectId}|${id}`;
        if (!id || now - (lastDemand.get(key) || 0) < DEMAND_INTERVAL_MS) continue;
        lastDemand.set(key, now);
        due.push(id);
    }
    if (due.length) void postJson(projectId, "/proxies/demand", { asset_ids: due.slice(0, 500) });
    return due;
}

/**
 * Whether a failed element's source really failed to decode rather than to load.
 * A 404, 5xx or refused connection fails a `<video src>` the same loud way as an
 * unsupported codec; only a source that answers proves the codec. Never throws.
 */
export async function confirmDecodeFailure(element) {
    const verdict = classifyMediaFailure(element);
    if (verdict !== "unconfirmed") return verdict === "decode";
    const src = String(element?.currentSrc || element?.src || "");
    if (!src) return false;
    try {
        const response = await fetch(src, { headers: { Range: "bytes=0-0" } });
        try { await response.body?.cancel?.(); } catch (_) {}
        return !!response.ok;
    } catch (_error) {
        return false;
    }
}

/**
 * A <video> failed to decode what the server allowed (`variant: "original"`), or a
 * proxy failed (`variant: "proxy"` with its rev). Call only with a confirmed
 * failure (`confirmDecodeFailure`). Marked in the store at once, so surfaces switch
 * to the notice, and reported once.
 */
export function reportDecodeFailure(projectId, asset, variant = "original", rev = "") {
    if (!asset?.asset_id || !projectId) return false;
    proxyStore.noteDecodeFailure(projectId, asset, variant, rev);
    const key = `${projectId}|${asset.asset_id}|${variant}|${variant === "proxy" ? rev : ""}`;
    if (reportedFailures.has(key)) return false;
    reportedFailures.add(key);
    const body = { asset_id: asset.asset_id, variant };
    if (variant === "proxy") body.rev = rev;
    void postJson(projectId, "/proxies/runtime-undecodable", body);
    return true;
}

/** Confirm, then report: for surfaces that only have the failed element. */
export async function confirmAndReportDecodeFailure(projectId, asset, element, variant = "original", rev = "") {
    if (!(await confirmDecodeFailure(element))) return false;
    reportDecodeFailure(projectId, asset, variant, rev);
    return true;
}

/** Tests only. */
export function _resetProxyClient() {
    lastReconcile.clear();
    lastDemand.clear();
    reportedFailures.clear();
    refreshes.clear();
}
