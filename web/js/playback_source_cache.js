function normalizeSourcePath(sourcePath) {
    return String(sourcePath || "").replace(/\\/g, "/").replace(/^\.\//, "");
}

function sourceRevision(asset) {
    if (!asset || typeof asset !== "object") return "unregistered";
    const assetId = String(asset.asset_id || "");
    const signature = String(asset.media_probe_signature || "");
    if (signature) return `${assetId}|${signature}`;
    return [
        assetId,
        `size:${Math.max(0, Number(asset.size_bytes) || 0)}`,
        `imported:${String(asset.imported_at || "")}`,
    ].join("|");
}

function assetForSourcePath(getAssetForSourcePath, rawPath, normalizedPath) {
    const candidates = [];
    const addCandidate = (value) => {
        const candidate = String(value || "");
        if (candidate && !candidates.includes(candidate)) candidates.push(candidate);
    };
    addCandidate(rawPath);
    addCandidate(normalizedPath);
    addCandidate(normalizedPath.replace(/\//g, "\\"));
    for (const candidate of candidates) {
        const asset = getAssetForSourcePath(candidate);
        if (asset) return asset;
    }
    return null;
}

function isAbortError(error, signal = null) {
    return !!signal?.aborted || error?.name === "AbortError";
}

export function _playbackSourceRevision(asset) {
    return sourceRevision(asset);
}

export function _normalizePlaybackSourcePath(sourcePath) {
    return normalizeSourcePath(sourcePath);
}

export function createPlaybackSourceCache({
    getAssetForSourcePath = () => null,
    getLiveSourcePaths = () => [],
    getStreamingMode = () => "auto",
    buildDirectUrl = () => null,
    fetchMedia = (...args) => fetch(...args),
    createObjectUrl = (blob) => URL.createObjectURL(blob),
    revokeObjectUrl = (url) => URL.revokeObjectURL(url),
    now = () => performance.now(),
    isDestroyed = () => false,
    isDiagnosticsEnabled = () => true,
    recordEvent = () => {},
} = {}) {
    const entries = new Map();
    let lastFetchCompletedAtMs = 0;
    let lastObjectUrlCreatedAtMs = 0;
    let lastEvictedAtMs = 0;

    const modeFor = (forceBlob = false) => (
        forceBlob || getStreamingMode() !== "direct" ? "blob" : "direct"
    );

    function identityFor(sourcePath, { forceBlob = false, mode = "" } = {}) {
        const rawPath = String(sourcePath || "");
        const normalizedPath = normalizeSourcePath(rawPath);
        if (!normalizedPath) return null;
        const resolvedMode = mode || modeFor(forceBlob);
        const revision = sourceRevision(assetForSourcePath(getAssetForSourcePath, rawPath, normalizedPath));
        return {
            sourcePath: normalizedPath,
            revision,
            mode: resolvedMode,
            sourceIdentity: `${normalizedPath}|${revision}`,
            cacheKey: `${resolvedMode}:${normalizedPath}|${revision}`,
        };
    }

    function currentLiveRevisions() {
        const revisions = new Map();
        for (const sourcePath of getLiveSourcePaths() || []) {
            const identity = identityFor(sourcePath);
            if (identity) revisions.set(identity.sourcePath, identity.revision);
        }
        return revisions;
    }

    function entryIsLive(entry, liveRevisions = null) {
        if (!entry) return false;
        const revisions = liveRevisions || currentLiveRevisions();
        return revisions.get(entry.sourcePath) === entry.revision;
    }

    function safeRecord(action, entry = null, details = {}) {
        if (!isDiagnosticsEnabled()) return;
        try {
            recordEvent({
                action,
                sourcePath: entry?.sourcePath || details.sourcePath || "",
                sourceIdentity: entry?.sourceIdentity || details.sourceIdentity || "",
                cacheKey: entry?.key || details.cacheKey || "",
                revision: entry?.revision || details.revision || "",
                mode: entry?.mode || details.mode || "",
                holderCount: entry?.holders?.size || 0,
                inFlight: !!entry?.inFlight,
                blobSize: Math.max(0, Number(entry?.blobSize) || 0),
                ...details,
            });
        } catch (_) {}
    }

    function revokeEntryUrl(entry) {
        if (!entry) return;
        if (entry.usesObjectUrl && entry.objectUrl) {
            try {
                revokeObjectUrl(entry.objectUrl);
            } catch (_) {}
        }
        entry.objectUrl = null;
        entry.usesObjectUrl = false;
        entry.blobSize = 0;
    }

    function finalizeEviction(entry, reason = "unused") {
        if (!entry || entry.evicted) return false;
        const evictedBlobSize = Math.max(0, Number(entry.blobSize) || 0);
        entry.evicted = true;
        if (entries.get(entry.key) === entry) entries.delete(entry.key);
        entry.pendingEviction = true;
        entry.evictionReason = reason;
        try {
            entry.abortController?.abort?.();
        } catch (_) {}
        revokeEntryUrl(entry);
        lastEvictedAtMs = now();
        safeRecord("evicted", entry, { reason, blobSize: evictedBlobSize });
        return true;
    }

    function markEntryObsolete(entry, reason, releaseHolders = null) {
        if (!entry || entry.pendingEviction) return;
        entry.live = false;
        entry.pendingEviction = true;
        entry.evictionReason = reason;
        const holders = Array.from(entry.holders || []);
        if (holders.length && typeof releaseHolders === "function") {
            for (const holder of holders) releaseHolders(holder);
        }
        if (!entry.holders?.size) {
            finalizeEviction(entry, reason);
        } else {
            safeRecord("eviction_pending", entry, { reason });
        }
    }

    function retireOutdatedPathEntries(identity, reason = "revision-replaced", releaseHolders = null) {
        for (const entry of Array.from(entries.values())) {
            if (entry.sourcePath !== identity.sourcePath) continue;
            if (entry.revision === identity.revision) continue;
            markEntryObsolete(entry, reason, releaseHolders);
        }
    }

    function cacheKeyFor(sourcePath, options = {}) {
        return identityFor(sourcePath, options)?.cacheKey || "";
    }

    async function resolve(sourcePath, { forceBlob = false, releaseHolders = null } = {}) {
        const identity = identityFor(sourcePath, { forceBlob });
        if (!identity || isDestroyed()) return null;
        retireOutdatedPathEntries(identity, "revision-replaced", releaseHolders);
        const cached = entries.get(identity.cacheKey);
        if (cached?.promise && !cached.pendingEviction) {
            cached.live = entryIsLive(cached);
            cached.lastUsedAtMs = now();
            safeRecord(cached.inFlight ? "cache_coalesced" : "cache_hit", cached);
            return cached.promise;
        }
        const directUrl = buildDirectUrl(identity.sourcePath);
        if (!directUrl) return null;
        const entry = {
            key: identity.cacheKey,
            sourcePath: identity.sourcePath,
            sourceIdentity: identity.sourceIdentity,
            revision: identity.revision,
            mode: identity.mode,
            holders: new Set(),
            objectUrl: null,
            usesObjectUrl: false,
            blobSize: 0,
            promise: null,
            abortController: typeof AbortController === "function" ? new AbortController() : null,
            inFlight: identity.mode === "blob",
            live: entryIsLive(identity),
            pendingEviction: false,
            evicted: false,
            evictionReason: "",
            createdAtMs: now(),
            lastUsedAtMs: now(),
        };
        entries.set(entry.key, entry);
        safeRecord("cache_miss", entry);
        entry.promise = Promise.resolve().then(async () => {
            if (entries.get(entry.key) !== entry || entry.pendingEviction || isDestroyed()) return null;
            if (entry.mode === "direct") {
                entry.objectUrl = directUrl;
                entry.lastUsedAtMs = now();
                safeRecord("direct_ready", entry);
                return { cacheKey: entry.key, url: directUrl };
            }
            const startedAtMs = now();
            safeRecord("fetch_started", entry);
            try {
                const response = await fetchMedia(directUrl, entry.abortController
                    ? { signal: entry.abortController.signal }
                    : undefined);
                if (!response?.ok) {
                    throw new Error(`Failed to fetch media: ${response?.status ?? "unknown"}`);
                }
                const blob = await response.blob();
                if (entries.get(entry.key) !== entry || entry.pendingEviction || isDestroyed()) return null;
                entry.objectUrl = createObjectUrl(blob);
                entry.usesObjectUrl = true;
                entry.blobSize = Math.max(0, Number(blob?.size) || 0);
                entry.inFlight = false;
                entry.lastUsedAtMs = now();
                lastFetchCompletedAtMs = entry.lastUsedAtMs;
                lastObjectUrlCreatedAtMs = entry.lastUsedAtMs;
                safeRecord("fetch_completed", entry, {
                    durationMs: Math.max(0, entry.lastUsedAtMs - startedAtMs),
                });
                return { cacheKey: entry.key, url: entry.objectUrl };
            } catch (error) {
                entry.inFlight = false;
                const aborted = isAbortError(error, entry.abortController?.signal);
                if (aborted) {
                    safeRecord("fetch_aborted", entry, {
                        durationMs: Math.max(0, now() - startedAtMs),
                        reason: entry.evictionReason || "aborted",
                    });
                    if (entries.get(entry.key) === entry && !entry.holders.size) {
                        finalizeEviction(entry, entry.evictionReason || "aborted");
                    }
                    return null;
                }
                if (entries.get(entry.key) !== entry || entry.pendingEviction || isDestroyed()) return null;
                safeRecord("fetch_failed", entry, {
                    durationMs: Math.max(0, now() - startedAtMs),
                    error: String(error?.message || error || ""),
                });
                entry.objectUrl = directUrl;
                entry.usesObjectUrl = false;
                entry.lastUsedAtMs = now();
                safeRecord("fallback_direct", entry, {
                    error: String(error?.message || error || ""),
                });
                return { cacheKey: entry.key, url: directUrl };
            }
        });
        return entry.promise;
    }

    function addHolder(cacheKey, holder) {
        const entry = entries.get(cacheKey);
        if (!entry || entry.pendingEviction || !holder) return false;
        entry.holders.add(holder);
        entry.lastUsedAtMs = now();
        return true;
    }

    function releaseHolder(cacheKey, holder) {
        const entry = entries.get(cacheKey);
        if (!entry) return false;
        entry.holders.delete(holder);
        entry.lastUsedAtMs = now();
        if (!entry.holders.size && (entry.pendingEviction || !entry.live)) {
            finalizeEviction(entry, entry.evictionReason || "source-not-live");
        }
        return true;
    }

    function releaseIfUnused(cacheKey, reason = "unused") {
        const entry = entries.get(cacheKey);
        if (!entry || entry.holders.size) return false;
        if (!entry.pendingEviction && entry.live) return false;
        return finalizeEviction(entry, entry.evictionReason || reason);
    }

    function reconcile(reason = "scene-refresh", { force = false, releaseHolders = null } = {}) {
        const liveRevisions = force ? new Map() : currentLiveRevisions();
        for (const entry of Array.from(entries.values())) {
            const live = !force && entryIsLive(entry, liveRevisions);
            entry.live = live;
            if (live) {
                entry.pendingEviction = false;
                entry.evictionReason = "";
                continue;
            }
            markEntryObsolete(entry, reason, releaseHolders);
        }
    }

    function clear(reason = "clear", { releaseHolders = null } = {}) {
        reconcile(reason, { force: true, releaseHolders });
        for (const entry of Array.from(entries.values())) finalizeEviction(entry, reason);
    }

    function snapshot(timestamp = now()) {
        let retainedBytes = 0;
        let heldEntries = 0;
        let idleEntries = 0;
        let inFlightEntries = 0;
        let pendingEvictionEntries = 0;
        for (const entry of entries.values()) {
            retainedBytes += Math.max(0, Number(entry.blobSize) || 0);
            if (entry.holders?.size) heldEntries += 1;
            else idleEntries += 1;
            if (entry.inFlight) inFlightEntries += 1;
            if (entry.pendingEviction) pendingEvictionEntries += 1;
        }
        const age = (value) => value > 0 ? Math.max(0, timestamp - value) : null;
        return {
            entryCount: entries.size,
            heldEntries,
            idleEntries,
            inFlightEntries,
            pendingEvictionEntries,
            retainedBytes,
            lastFetchCompletedAgeMs: age(lastFetchCompletedAtMs),
            lastObjectUrlCreatedAgeMs: age(lastObjectUrlCreatedAtMs),
            lastEvictedAgeMs: age(lastEvictedAtMs),
        };
    }

    return {
        entries,
        identityFor,
        cacheKeyFor,
        resolve,
        addHolder,
        releaseHolder,
        releaseIfUnused,
        reconcile,
        clear,
        snapshot,
    };
}
