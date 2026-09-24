// Per-gallery provenance detail loading: a bounded cache keyed by asset id and provenance
// revision, the selected-first + following-window demand each inspection surface makes,
// and background preloading of assets that arrive while the gallery is mounted.
//
// DOM-free by design: the gallery owns rendering and navigation order, the asset refresh
// coordinator owns transport, and this module owns only which details are wanted, which are
// held, and whether an arrival still matches the list the gallery shows. Details are
// ephemeral gallery data; they never attach to the lean list records and never become a
// write authority.

import { ASSET_DETAIL_BATCH_LIMIT } from "./asset_refresh_coordinator.js";

// The selected asset plus its window share one route batch.
export const PRELOAD_FOLLOWING_LIMIT = ASSET_DETAIL_BATCH_LIMIT - 1;
export const PRELOAD_NEW_ASSETS_LIMIT = ASSET_DETAIL_BATCH_LIMIT - 1;

const MISMATCH_MESSAGE = "Metadata changed while loading.";

// True when the gallery must fetch this asset's details before showing them. A record
// that already carries `generation_params` (a single-asset response) is complete; an old
// server that predates `has_provenance` omits the flag, which counts as "may have some".
export function assetNeedsDetail(asset) {
    if (!asset || !asset.asset_id || !asset.provenance_revision) return false;
    if (asset.generation_params && typeof asset.generation_params === "object") return false;
    return asset.has_provenance !== false;
}

// Ids after `currentId` in `orderedIds`, at most `count`. `wrap` follows surfaces whose
// navigation cycles (the inspect overlay); the gallery list stops at its end.
export function followingIds(orderedIds, currentId, count, { wrap = false } = {}) {
    const limit = Math.max(0, Math.floor(Number(count) || 0));
    const index = orderedIds.indexOf(currentId);
    if (!limit || index < 0) return [];
    const result = [];
    for (let step = 1; step < orderedIds.length && result.length < limit; step += 1) {
        const position = index + step;
        if (position >= orderedIds.length && !wrap) break;
        result.push(orderedIds[position % orderedIds.length]);
    }
    return result;
}

// A positive cache cap also bounds the window: preloading past it evicts its own work.
export function effectivePreloadCount({ preloadFollowingAssets = 0, maxCachedProvenanceDetails = 0 } = {}) {
    const preload = Math.min(PRELOAD_FOLLOWING_LIMIT, Math.max(0, Math.floor(Number(preloadFollowingAssets) || 0)));
    const cap = Math.max(0, Math.floor(Number(maxCachedProvenanceDetails) || 0));
    return cap > 0 ? Math.min(preload, cap) : preload;
}

// LRU of reusable details, one revision per asset. Displayed assets are held outside the
// cap, so side-by-side compare survives a cap of 1.
export function createDetailCache({ limit = 0 } = {}) {
    const entries = new Map();
    let displayed = new Set();
    let cap = 0;
    const normalize = (value) => Math.max(0, Math.floor(Number(value) || 0));
    const evict = () => {
        if (!cap) return;
        let reusable = 0;
        for (const id of entries.keys()) if (!displayed.has(id)) reusable += 1;
        for (const id of entries.keys()) {
            if (reusable <= cap) break;
            if (displayed.has(id)) continue;
            entries.delete(id);
            reusable -= 1;
        }
    };
    cap = normalize(limit);
    return {
        get(assetId, revision) {
            const entry = entries.get(assetId);
            if (!entry || entry.revision !== revision) return undefined;
            entries.delete(assetId);
            entries.set(assetId, entry);
            return entry.value;
        },
        // Read without counting as a use: search walks the whole list and must not reorder
        // the LRU into list order.
        peek(assetId, revision) {
            const entry = entries.get(assetId);
            return entry && entry.revision === revision ? entry.value : undefined;
        },
        has(assetId, revision) {
            return entries.get(assetId)?.revision === revision;
        },
        set(assetId, revision, value) {
            entries.delete(assetId);
            entries.set(assetId, { revision, value });
            evict();
        },
        setDisplayed(assetIds) {
            displayed = new Set(assetIds);
            evict();
        },
        setLimit(value) {
            cap = normalize(value);
            evict();
        },
        // Keeps only entries whose asset is still listed at the same revision.
        retain(revisionsById) {
            for (const [id, entry] of entries) {
                if (revisionsById.get(id) !== entry.revision) entries.delete(id);
            }
        },
        clear() {
            entries.clear();
            displayed = new Set();
        },
        get size() {
            return entries.size;
        },
        ids() {
            return [...entries.keys()];
        },
    };
}

function versionIsOlder(served, snapshot) {
    return !!served && !!snapshot && String(served) < String(snapshot);
}

export function createDetailLoader({
    ownerId,
    requestDetails,
    cancelDetails,
    requestListRefresh = async () => {},
    onChange = () => {},
    settings = {},
    diagnosticRecorder = null,
} = {}) {
    const owner = String(ownerId || "gallery");
    let projectId = "";
    let generation = 0;
    let destroyed = false;
    let listVersion = "";
    let listLoaded = false;
    let knownIds = new Set();
    let listRevisions = new Map();
    let preload = effectivePreloadCount(settings);
    let preloadNew = settings.preloadNewAssets !== false;
    let cacheCap = Math.max(0, Math.floor(Number(settings.maxCachedProvenanceDetails) || 0));
    const cache = createDetailCache({ limit: cacheCap });
    // assetId -> { revision, rank } for reads this gallery is waiting on.
    const pending = new Map();
    // Failures are remembered per revision: a displayed failure shows a retryable error,
    // a preload failure only stops the window from asking again until the revision moves.
    const errors = new Map();
    const preloadFailed = new Map();
    const mismatchRetried = new Map();
    // assetId -> revision whose list refresh is under way. Counts as waiting: the second
    // read must follow the refresh, or it only repeats the mismatch against the old list.
    const refreshing = new Map();
    // Preload failures with no HTTP status (a dropped connection) are forgotten as soon as
    // any read succeeds again, so a network blip does not stop preloading until revisions move.
    const transientFailures = new Set();
    let lastCurrent = null;
    let lastWindowKey = "";
    let changed = new Set();
    let flushScheduled = false;

    const RANK = { selected: 0, prefetch: 2, background: 3 };

    const notify = (assetId) => {
        changed.add(assetId);
        if (flushScheduled) return;
        flushScheduled = true;
        queueMicrotask(() => {
            flushScheduled = false;
            const ids = [...changed];
            changed = new Set();
            if (!destroyed && ids.length) onChange(ids);
        });
    };

    // The coordinator settles withdrawn waiters synchronously, inside this call, so by the
    // time it returns `pending` no longer names them and `sync` may ask for them again.
    const cancelOwned = (priorities = null) => {
        if (projectId) cancelDetails({ projectId, ownerId: owner, priorities });
    };

    const mismatch = (assetId, revision, rank, served) => {
        if (rank !== RANK.selected) {
            preloadFailed.set(assetId, revision);
            return;
        }
        if (mismatchRetried.get(assetId) === revision) {
            errors.set(assetId, { revision, message: MISMATCH_MESSAGE });
            notify(assetId);
            return;
        }
        // The list this gallery shows is older than the asset: refresh it, then ask once
        // more. Whether or not the host adopts a new list, settling re-syncs demand.
        mismatchRetried.set(assetId, revision);
        refreshing.set(assetId, revision);
        const captured = generation;
        Promise.resolve()
            .then(() => requestListRefresh({ requiredVersion: served || "", reason: "gallery_detail_mismatch" }))
            .catch(() => {})
            .then(() => {
                if (captured !== generation) return;
                if (refreshing.get(assetId) === revision) refreshing.delete(assetId);
                notify(assetId);
            });
    };

    const request = (assetIds, priority) => {
        if (!assetIds.length || !projectId) return;
        const rank = RANK[priority];
        const captured = generation;
        const revisions = new Map(assetIds.map((id) => [id, listRevisions.get(id)]));
        for (const id of assetIds) pending.set(id, { revision: revisions.get(id), rank });
        requestDetails({
            projectId,
            kind: "provenance",
            assetIds,
            priority,
            ownerId: owner,
            diagnosticRecorder,
            onResult: (assetId, result) => {
                if (destroyed || captured !== generation) return;
                const requested = revisions.get(assetId);
                const waiting = pending.get(assetId);
                if (result.status === "cancelled") {
                    if (waiting?.rank === rank && waiting.revision === requested) pending.delete(assetId);
                    return;
                }
                if (waiting?.revision === requested) pending.delete(assetId);
                // The effective priority is the best one anyone here asked for, so a preload
                // that a selection joined reports its failure as a selection failure.
                const effectiveRank = Math.min(rank, waiting?.rank ?? rank);
                const listed = listRevisions.get(assetId);
                if (result.status === "ok") {
                    if (result.revision === requested && listed === requested
                        && !versionIsOlder(result.modifiedAt, listVersion)) {
                        cache.set(assetId, requested, result.value && typeof result.value === "object" ? result.value : {});
                        errors.delete(assetId);
                        preloadFailed.delete(assetId);
                        for (const id of transientFailures) preloadFailed.delete(id);
                        transientFailures.clear();
                        notify(assetId);
                        return;
                    }
                    if (listed !== requested) return; // the list moved on; the new revision is asked for on sync
                    mismatch(assetId, requested, effectiveRank, result.modifiedAt);
                    return;
                }
                if (result.status === "missing") {
                    if (listed === requested) mismatch(assetId, requested, effectiveRank, result.modifiedAt);
                    return;
                }
                if (effectiveRank === RANK.selected) {
                    errors.set(assetId, { revision: requested,
                        message: result.error?.message || "Metadata could not be loaded." });
                    notify(assetId);
                } else {
                    preloadFailed.set(assetId, requested);
                    if (!(Number(result.error?.status) >= 400)) transientFailures.add(assetId);
                }
            },
        });
    };

    const isHeld = (asset) => cache.has(asset.asset_id, asset.provenance_revision);
    const isWaiting = (asset, rank = RANK.background) => {
        if (refreshing.get(asset.asset_id) === asset.provenance_revision) return true;
        const waiting = pending.get(asset.asset_id);
        return !!waiting && waiting.revision === asset.provenance_revision && waiting.rank <= rank;
    };
    // `errors` holds {revision, message}; the preload and retry maps hold the bare revision.
    const failed = (map, asset) => {
        const value = map.get(asset.asset_id);
        return (typeof value === "string" ? value : value?.revision) === asset.provenance_revision;
    };

    const clearState = () => {
        cache.clear();
        pending.clear();
        errors.clear();
        preloadFailed.clear();
        mismatchRetried.clear();
        refreshing.clear();
        transientFailures.clear();
    };

    return {
        // Adopt a list. A project change resets everything; a list carrying its snapshot
        // version counts as loaded, and ids a loaded list did not have are new arrivals.
        listChanged({ projectId: nextProjectId, assets = [], version = "" }) {
            if (destroyed) return;
            const normalized = String(nextProjectId || "");
            if (normalized !== projectId) {
                cancelOwned();
                generation += 1;
                projectId = normalized;
                clearState();
                listLoaded = false;
                listVersion = "";
                knownIds = new Set();
                lastCurrent = null;
                lastWindowKey = "";
            }
            const revisions = new Map();
            for (const asset of assets) {
                if (assetNeedsDetail(asset)) revisions.set(asset.asset_id, asset.provenance_revision);
            }
            listRevisions = revisions;
            // An unversioned list after a loaded one (a host seed, never a server list) is
            // shown but decides nothing: held details, failures and known ids wait for the
            // next real list rather than being discarded or turned into arrivals.
            if (listLoaded && !version) return;
            cache.retain(revisions);
            for (const id of [...transientFailures]) if (!revisions.has(id)) transientFailures.delete(id);
            for (const map of [errors, preloadFailed, mismatchRetried, refreshing]) {
                for (const [id, value] of map) {
                    const revision = typeof value === "string" ? value : value.revision;
                    if (revisions.get(id) !== revision) map.delete(id);
                }
            }
            const arrivals = listLoaded && version && preloadNew
                ? assets.filter((asset) => !knownIds.has(asset.asset_id) && assetNeedsDetail(asset))
                : [];
            knownIds = new Set(assets.map((asset) => asset.asset_id));
            if (version) {
                listVersion = String(version);
                listLoaded = true;
            }
            // A positive cache cap is shared with the preload window: arrivals get only the
            // room the window leaves, or each would evict the other and both be read again.
            const room = cacheCap > 0 ? Math.max(0, cacheCap - preload) : PRELOAD_NEW_ASSETS_LIMIT;
            if (arrivals.length && room > 0) {
                const importedAt = (asset) => Date.parse(asset?.imported_at || "") || 0;
                const newest = arrivals
                    .filter((asset) => !isHeld(asset) && !isWaiting(asset))
                    .sort((a, b) => importedAt(b) - importedAt(a))
                    .slice(0, Math.min(room, PRELOAD_NEW_ASSETS_LIMIT))
                    .map((asset) => asset.asset_id);
                request(newest, "background");
            }
        },

        // One inspection surface's demand: the assets on screen first, then the window
        // following `currentId` in that surface's own navigation order.
        sync({ projectId: shownProjectId, displayed = [], order = [], currentId = "", wrap = false } = {}) {
            // A gallery that switched project but has not adopted the new list yet shows
            // the old project's assets; ask nothing for them.
            if (destroyed || !projectId || String(shownProjectId || "") !== projectId) return;
            const shown = displayed.filter(Boolean);
            cache.setDisplayed(shown.map((asset) => asset.asset_id));
            const window = followingIds(order.map((asset) => asset.asset_id), currentId, preload, { wrap });
            const windowKey = `${currentId}\u0000${wrap}\u0000${window.join("\u0000")}`;
            if (lastCurrent !== null && windowKey !== lastWindowKey) {
                // New selections outrank unstarted preload work; in-flight reads finish.
                cancelOwned(["selected", "prefetch"]);
            }
            lastCurrent = currentId;
            lastWindowKey = windowKey;
            const selected = shown
                .filter((asset) => assetNeedsDetail(asset) && listRevisions.get(asset.asset_id) === asset.provenance_revision)
                .filter((asset) => !isHeld(asset) && !failed(errors, asset) && !isWaiting(asset, RANK.selected))
                .map((asset) => asset.asset_id);
            request([...new Set(selected)], "selected");
            const shownIds = new Set(shown.map((asset) => asset.asset_id));
            const byId = new Map(order.map((asset) => [asset.asset_id, asset]));
            const following = window
                .map((id) => byId.get(id))
                .filter((asset) => asset && !shownIds.has(asset.asset_id) && assetNeedsDetail(asset)
                    && listRevisions.get(asset.asset_id) === asset.provenance_revision)
                .filter((asset) => !isHeld(asset) && !failed(preloadFailed, asset) && !isWaiting(asset, RANK.prefetch))
                .map((asset) => asset.asset_id);
            request(following, "prefetch");
        },

        // ready: params to render; loading: a read is due or under way; error: retryable.
        resolve(asset) {
            if (!assetNeedsDetail(asset)) {
                const params = asset?.generation_params;
                return { status: "ready", params: params && typeof params === "object" ? params : {} };
            }
            const value = cache.get(asset.asset_id, asset.provenance_revision);
            if (value !== undefined) return { status: "ready", params: value };
            const error = errors.get(asset.asset_id);
            if (error?.revision === asset.provenance_revision) return { status: "error", message: error.message };
            return { status: "loading" };
        },

        // A held detail at exactly this revision, without touching the LRU order.
        peek(assetId, revision) {
            return cache.peek(assetId, revision);
        },

        retry(asset) {
            if (!asset) return;
            errors.delete(asset.asset_id);
            mismatchRetried.delete(asset.asset_id);
            preloadFailed.delete(asset.asset_id);
            notify(asset.asset_id);
        },

        // Returns whether demand changed: only a new window size or cap re-plans the window,
        // so an unrelated settings change does not withdraw and re-queue queued reads.
        applySettings(next = {}) {
            const nextPreload = effectivePreloadCount(next);
            const nextCap = Math.max(0, Math.floor(Number(next.maxCachedProvenanceDetails) || 0));
            preloadNew = next.preloadNewAssets !== false;
            const changedDemand = nextPreload !== preload || nextCap !== cacheCap;
            preload = nextPreload;
            cacheCap = nextCap;
            cache.setLimit(nextCap);
            if (changedDemand) lastWindowKey = "";
            return changedDemand;
        },

        destroy() {
            if (destroyed) return;
            cancelOwned();
            destroyed = true;
            generation += 1;
            clearState();
        },

        _debug() {
            return {
                projectId,
                listVersion,
                listLoaded,
                cached: cache.ids(),
                pending: [...pending.entries()].map(([id, entry]) => [id, entry.rank]),
                errors: [...errors.keys()],
                preloadFailed: [...preloadFailed.keys()],
                refreshing: [...refreshing.keys()],
            };
        },
    };
}

// Complete `tracked:`/`field:` search needs every asset's tracked metadata at the revision
// the gallery lists, so it is built into its own projection rather than read from the
// detail cache: eviction there must never change a search result. Search reports
// "loading" until every listed asset is accounted for; it never matches a partial set.
export function createSearchProjection({
    ownerId,
    requestDetails,
    cancelDetails,
    requestListRefresh = async () => {},
    onChange = () => {},
    peekDetail = () => undefined,
    diagnosticRecorder = null,
} = {}) {
    const owner = String(ownerId || "gallery-search");
    let projectId = "";
    let destroyed = false;
    let generation = 0;
    let build = 0;
    // assetId -> { revision, value } for entries adopted at a listed revision.
    const entries = new Map();
    let target = new Map();
    let targetKey = "";
    let status = "idle";
    let message = "";
    // The target must be (re)built on the next ensure even though its key is unchanged:
    // initially, after Retry, after a restart's list refresh settles, and after idle().
    let stale = true;
    // A mismatch refreshes the list and restarts once per target: `restartedKey` names the
    // target that spent it, and while the refresh is under way the search stays loading.
    let restartedKey = null;
    let restartPending = false;
    // The status the gallery last learned, from an ensure return or a change event, so a
    // change event fires only for a transition it has not already rendered.
    let reported = "idle";
    // A build nobody asks for any more (idle) keeps adopting its in-flight results, which
    // are valid for their revisions, but no longer decides the status.
    let idledBuild = -1;

    const emitChange = ({ force = false } = {}) => {
        queueMicrotask(() => {
            if (destroyed || (!force && status === reported)) return;
            reported = status;
            onChange(status);
        });
    };

    const withdraw = (priorities = null) => {
        if (projectId) cancelDetails({ projectId, ownerId: owner, priorities });
    };

    const resetFor = (nextProjectId) => {
        withdraw();
        generation += 1;
        build += 1;
        projectId = nextProjectId;
        entries.clear();
        target = new Map();
        targetKey = "";
        status = "idle";
        message = "";
        stale = true;
        restartedKey = null;
        restartPending = false;
    };

    const fail = (text) => {
        status = "error";
        message = text;
        emitChange();
    };

    const complete = () => {
        for (const [id, revision] of target) {
            if (entries.get(id)?.revision !== revision) return false;
        }
        return true;
    };

    const start = (listVersion) => {
        build += 1;
        const buildId = build;
        // A superseded build's unstarted batches are withdrawn; its in-flight one finishes
        // and is ignored by build id.
        withdraw(["search"]);
        const captured = generation;
        const missing = [];
        for (const [id, revision] of target) {
            if (entries.get(id)?.revision === revision) continue;
            const detail = peekDetail(id, revision);
            if (detail !== undefined) {
                entries.set(id, { revision, value: detail?.editor_export?.tracked_metadata });
                continue;
            }
            missing.push(id);
        }
        if (!missing.length) {
            status = "ready";
            message = "";
            emitChange();
            return;
        }
        status = "loading";
        message = "";
        let mismatched = false;
        let failure = "";
        let served = "";
        const expected = new Map(missing.map((id) => [id, target.get(id)]));
        const handle = requestDetails({
            projectId,
            kind: "search",
            assetIds: missing,
            priority: "search",
            ownerId: owner,
            diagnosticRecorder,
            onResult: (assetId, result) => {
                if (destroyed || captured !== generation || buildId !== build) return;
                if (result.status === "ok" && result.revision === expected.get(assetId)
                    && !versionIsOlder(result.modifiedAt, listVersion)) {
                    entries.set(assetId, { revision: result.revision, value: result.value });
                    return;
                }
                if (result.status === "ok" || result.status === "missing") {
                    mismatched = true;
                    served = result.modifiedAt || served;
                    return;
                }
                if (result.status === "error") failure = failure || result.error?.message || "Metadata could not be loaded.";
            },
        });
        handle.promise.then(() => {
            if (destroyed || captured !== generation || buildId !== build || buildId === idledBuild) return;
            if (failure) {
                fail(`Metadata search unavailable: ${failure}`);
                return;
            }
            if (mismatched) {
                if (restartedKey === targetKey) {
                    fail("Metadata search unavailable: assets changed while loading.");
                    return;
                }
                // Refresh the list and restart once for this target. Renders meanwhile keep
                // the search loading instead of spending the restart on the old list.
                restartedKey = targetKey;
                restartPending = true;
                Promise.resolve()
                    .then(() => requestListRefresh({ requiredVersion: served, reason: "gallery_search_mismatch" }))
                    .catch(() => {})
                    .then(() => {
                        if (captured !== generation || !restartPending) return;
                        restartPending = false;
                        stale = true;
                        emitChange({ force: true });
                    });
                return;
            }
            // Ready means every listed asset was adopted at its listed revision; anything
            // less (a withdrawn batch) is a partial projection and never answers a search.
            if (!complete()) {
                fail("Metadata search was interrupted.");
                return;
            }
            status = "ready";
            emitChange();
        });
    };

    return {
        // Called once per render while a metadata query is active. Cheap when the listed
        // ids and revisions are unchanged; otherwise (re)builds only what moved.
        ensure({ projectId: nextProjectId, assets = [], version = "" }) {
            if (destroyed) return status;
            const normalized = String(nextProjectId || "");
            if (normalized !== projectId) resetFor(normalized);
            if (!projectId) return status;
            const next = new Map();
            for (const asset of assets) {
                if (assetNeedsDetail(asset)) next.set(asset.asset_id, asset.provenance_revision);
            }
            const key = [...next].map(([id, revision]) => `${id}:${revision}`).join("|");
            if (key === targetKey) {
                if (restartPending || !stale) {
                    reported = status;
                    return status;
                }
            } else {
                // A new list supersedes a pending restart; its own mismatch may restart once.
                restartPending = false;
                target = next;
                targetKey = key;
                for (const id of [...entries.keys()]) {
                    if (!next.has(id)) entries.delete(id);
                }
            }
            stale = false;
            start(version);
            reported = status;
            return status;
        },

        // No surface has a metadata query any more: withdraw queued batches so they stop
        // outranking preloads. Adopted entries stay valid for their revisions.
        idle() {
            if (destroyed || status !== "loading") return;
            withdraw(["search"]);
            idledBuild = build;
            restartPending = false;
            status = "idle";
            stale = true;
            reported = status;
        },

        // Raw `tracked_metadata` value for an asset that needs fetching; `undefined` when not
        // adopted at the asset's listed revision (never the case once status is ready).
        valueFor(asset) {
            const entry = entries.get(asset?.asset_id);
            return entry && entry.revision === asset.provenance_revision ? entry.value : undefined;
        },

        get status() {
            return status;
        },

        get message() {
            return message;
        },

        retry() {
            if (status !== "error") return;
            status = "idle";
            message = "";
            restartedKey = null;
            stale = true;
            emitChange({ force: true });
        },

        destroy() {
            if (destroyed) return;
            withdraw();
            destroyed = true;
            generation += 1;
            entries.clear();
        },
    };
}
