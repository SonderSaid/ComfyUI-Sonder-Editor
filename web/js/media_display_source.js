// Which picture a surface shows for a video asset: its original, its proxy, or a
// notice ("blocked") because the original cannot play here and no proxy is ready.
// DOM-free leaf, the only authority on that choice in the page.
//
// The server decides whether an original needs a proxy (`server/proxy_media.py`)
// and reports it as `asset.proxy = { decision, state, rev, epoch }`, in the lean
// `/assets` list, in `GET /proxies` and in `proxy_status` events. This module keeps
// the newest of those per asset (by epoch, so a late list never undoes an event)
// and adds what only the browser can know: an original or a proxy that failed to
// decode here.
//
// One page can hold several projects (a dormant card per Editor node, a project
// switch), and copied projects share asset ids, so everything here is keyed by the
// project id the host uses in its URLs (the project folder's name).
//
// Render, export, bridges and frame grabs always use the original; only display
// surfaces come through here.

import { shouldSkipVideoLoad } from "./media_preview_support.js";
import { assetSourceRevision } from "./playback_source_cache.js";

// Decisions under which the original must not be handed to a <video>. "manual" is
// a proxy the user asked for on an original that plays: show the proxy once it is
// ready, the original until then.
const NEEDS_PROXY_DECISIONS = new Set(["undecodable", "runtime"]);

function proxyOf(value) {
    return value && typeof value === "object" ? value : null;
}

/**
 * Newest wins by `epoch`; on a tie the current value stays. An undecided
 * ("pending") decision never replaces a known one: the other fields still move.
 */
export function mergeProxyState(current, incoming) {
    const now = proxyOf(current);
    const next = proxyOf(incoming);
    if (!next) return now;
    if (!now) return { ...next };
    if ((Number(next.epoch) || 0) <= (Number(now.epoch) || 0)) return now;
    const merged = { ...next };
    if ((!merged.decision || merged.decision === "pending") && now.decision && now.decision !== "pending") {
        merged.decision = now.decision;
    }
    return merged;
}

// ── The page's store ──────────────────────────────────────────────────────────

const entries = new Map();          // `${project}\n${asset_id}` -> proxy state
// `${project}\n${asset_id}\n${source revision}\n${variant}\n${rev}` that failed to
// decode here. The source revision lets a Replace (new bytes) play again.
const localFailures = new Set();
const listeners = new Set();
let notifyScheduled = false;
let pendingChanges = new Map();     // project -> Set(asset_id)

function projectKeyOf(projectId) {
    return String(projectId || "");
}

function entryKey(projectId, assetId) {
    return `${projectKeyOf(projectId)}\n${String(assetId || "")}`;
}

function failureKey(projectId, asset, variant, rev) {
    return [projectKeyOf(projectId), String(asset?.asset_id || ""), assetSourceRevision(asset),
        variant, variant === "proxy" ? String(rev || "") : ""].join("\n");
}

function scheduleNotify(projectId, assetIds) {
    const key = projectKeyOf(projectId);
    if (!pendingChanges.has(key)) pendingChanges.set(key, new Set());
    for (const id of assetIds) pendingChanges.get(key).add(id);
    if (notifyScheduled) return;
    notifyScheduled = true;
    // One notification per frame however many events arrive.
    const run = () => {
        notifyScheduled = false;
        const changes = pendingChanges;
        pendingChanges = new Map();
        for (const listener of Array.from(listeners)) {
            try { listener(changes); } catch (error) { console.warn("[Sonder] proxy listener failed:", error); }
        }
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(run);
    else setTimeout(run, 0);
}

function ingestOne(projectId, assetId, proxy, changed) {
    const id = String(assetId || "");
    if (!projectKeyOf(projectId) || !id || !proxyOf(proxy)) return;
    const key = entryKey(projectId, id);
    const current = entries.get(key) || null;
    const merged = mergeProxyState(current, proxy);
    if (merged !== current) {
        entries.set(key, merged);
        changed.push(id);
    }
}

function ingestAll(projectId, pairs) {
    const changed = [];
    for (const [assetId, proxy] of pairs) ingestOne(projectId, assetId, proxy, changed);
    if (changed.length) scheduleNotify(projectId, changed);
    return changed;
}

export const proxyStore = {
    /** `proxy_status` items: `[{ asset_id, decision, state, rev, epoch, ... }]`. */
    ingestItems(projectId, items = []) {
        return ingestAll(projectId, (items || []).map((item) => [item?.asset_id, item]));
    },
    /** Asset payloads that carry `proxy` (the lean list, an import response). */
    ingestAssets(projectId, assets = []) {
        return ingestAll(projectId, (assets || []).map((asset) => [asset?.asset_id, asset?.proxy]));
    },
    /** `GET /proxies`: `{ items: { asset_id: proxy } }`. */
    ingestOverview(projectId, payload) {
        return ingestAll(projectId, Object.entries(payload?.items || {}));
    },
    get(projectId, assetId) {
        return entries.get(entryKey(projectId, assetId)) || null;
    },
    /**
     * `listener(changes: Map<projectId, Set<assetId>>)`, coalesced per animation
     * frame. Returns unsubscribe.
     */
    subscribe(listener) {
        if (typeof listener !== "function") return () => {};
        listeners.add(listener);
        return () => listeners.delete(listener);
    },
    /** A `<video>` of this asset's original (or of proxy `rev`) failed to decode here. */
    noteDecodeFailure(projectId, asset, variant = "original", rev = "") {
        const id = String(asset?.asset_id || "");
        if (!projectKeyOf(projectId) || !id) return false;
        const key = failureKey(projectId, asset, variant, rev);
        if (localFailures.has(key)) return false;
        localFailures.add(key);
        scheduleNotify(projectId, [id]);
        return true;
    },
    decodeFailed(projectId, asset, variant = "original", rev = "") {
        return !!asset?.asset_id && localFailures.has(failureKey(projectId, asset, variant, rev));
    },
    /** Tests only. */
    _reset() {
        entries.clear();
        localFailures.clear();
        listeners.clear();
        pendingChanges = new Map();
        notifyScheduled = false;
    },
};

/** The asset ids of `projectId` in a listener's `changes`, or an empty set. */
export function changedAssetIds(changes, projectId) {
    return changes?.get?.(projectKeyOf(projectId)) || new Set();
}

/** The newest proxy state known for an asset: its payload's and the store's. */
export function effectiveProxy(projectId, asset) {
    if (!asset || typeof asset !== "object") return null;
    return mergeProxyState(proxyOf(asset.proxy), proxyStore.get(projectId, asset.asset_id));
}

function isVideoAsset(asset) {
    return !!asset && typeof asset === "object" && (asset.asset_type || "video") === "video";
}

/**
 * Whether this asset's original must not reach a <video>: the server says so, or it
 * already failed here. With no server decision yet, the recorded-codec hint decides.
 */
export function needsProxy(projectId, asset) {
    if (!isVideoAsset(asset)) return false;
    if (proxyStore.decodeFailed(projectId, asset, "original")) return true;
    const decision = String(effectiveProxy(projectId, asset)?.decision || "");
    if (NEEDS_PROXY_DECISIONS.has(decision)) return true;
    if (!decision || decision === "pending") return shouldSkipVideoLoad(asset);
    return false;
}

/**
 * `{ variant: "proxy" | "original" | "blocked", rev, url }`. A ready proxy is always
 * preferred; "blocked" means: create no <video>, show the notice instead.
 * `urls.original(asset)` and `urls.proxy(asset, rev)` build URLs when given.
 */
export function displayVideoSource(projectId, asset, urls = {}) {
    if (!isVideoAsset(asset)) {
        return { variant: "original", rev: "", url: urls.original ? urls.original(asset) : null };
    }
    const proxy = effectiveProxy(projectId, asset);
    const rev = String(proxy?.rev || "");
    if (proxy?.state === "ready" && rev && !proxyStore.decodeFailed(projectId, asset, "proxy", rev)) {
        return { variant: "proxy", rev, url: urls.proxy ? urls.proxy(asset, rev) : null };
    }
    if (needsProxy(projectId, asset)) return { variant: "blocked", rev: "", url: null };
    return { variant: "original", rev: "", url: urls.original ? urls.original(asset) : null };
}

/**
 * What a blocked asset is waiting for, with copy for a notice:
 * `{ state, title, detail, waitingSince }`. `state` is one of waiting_idle, building,
 * queued, manual_only, failed, runtime.
 */
export function blockedState(projectId, asset) {
    const proxy = effectiveProxy(projectId, asset) || {};
    const localProxyFailure = proxy.rev && proxyStore.decodeFailed(projectId, asset, "proxy", proxy.rev);
    let state = String(proxy.state || "");
    if (localProxyFailure) state = "failed";
    else if (state === "ready" || state === "none" || state === "missing" || !state) state = "queued";
    if (state !== "failed" && proxyStore.decodeFailed(projectId, asset, "original")
        && !["building", "waiting_idle", "manual_only"].includes(state)) {
        state = "runtime";
    }
    const copy = BLOCKED_COPY[state] || BLOCKED_COPY.queued;
    return {
        state: BLOCKED_COPY[state] ? state : "queued",
        title: copy.title,
        detail: copy.detail,
        waitingSince: Number(proxy.waiting_since) || 0,
    };
}

const BLOCKED_COPY = {
    building: {
        title: "Building proxy…",
        detail: "This video can't play in the browser. A playable copy is being made.",
    },
    waiting_idle: {
        title: "Proxy waiting for ComfyUI",
        detail: "It builds once ComfyUI has been idle for 30 seconds.",
    },
    queued: {
        title: "Proxy queued",
        detail: "This video can't play in the browser. A playable copy is queued.",
    },
    manual_only: {
        title: "No proxy yet",
        // Both "Only when I ask" and a proxy the user deleted read manual_only.
        detail: "This video can't play in the browser, and its proxy is built only when asked.",
    },
    failed: {
        title: "Proxy failed",
        detail: "A playable copy of this video could not be made.",
    },
    runtime: {
        title: "Can't play this video here",
        detail: "This browser could not decode it, so it needs a playable copy.",
    },
};
