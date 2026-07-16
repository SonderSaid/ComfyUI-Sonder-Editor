// Shared ownership state for graph-card media that must yield while a fullscreen
// editor owns the page. This module deliberately contains no editor or node UI so
// both lifecycle shells can depend on it without creating an import cycle.

const suppressionOwners = new Set();
const suppressionListeners = new Set();
const previewDiagnostics = new Set();

function notifySuppressionListeners() {
    const suppressed = suppressionOwners.size > 0;
    for (const listener of Array.from(suppressionListeners)) {
        try {
            listener(suppressed);
        } catch (_) {
            // A stale preview must not break ownership release for the rest.
        }
    }
}

export function acquireGraphPreviewSuppression(owner = null) {
    const token = owner || Symbol("graph-preview-owner");
    const wasSuppressed = suppressionOwners.size > 0;
    suppressionOwners.add(token);
    if (!wasSuppressed) notifySuppressionListeners();
    let released = false;
    return () => {
        if (released) return;
        released = true;
        const wasActive = suppressionOwners.size > 0;
        suppressionOwners.delete(token);
        if (wasActive && suppressionOwners.size === 0) notifySuppressionListeners();
    };
}

export function subscribeGraphPreviewSuppression(listener) {
    if (typeof listener !== "function") return () => {};
    suppressionListeners.add(listener);
    listener(suppressionOwners.size > 0);
    return () => suppressionListeners.delete(listener);
}

export function graphPreviewSuppressionActive() {
    return suppressionOwners.size > 0;
}

export function registerGraphPreviewDiagnostic(getSnapshot) {
    if (typeof getSnapshot !== "function") return () => {};
    previewDiagnostics.add(getSnapshot);
    return () => previewDiagnostics.delete(getSnapshot);
}

function incrementCount(target, key) {
    const normalized = String(key || "unknown");
    target[normalized] = (target[normalized] || 0) + 1;
}

export function snapshotGraphPreviewDiagnostics() {
    const result = {
        total: 0,
        playing: 0,
        playingHidden: 0,
        suspended: 0,
        hibernated: 0,
        roles: {},
        suspensionReasons: {},
    };
    for (const getSnapshot of Array.from(previewDiagnostics)) {
        let entry = null;
        try {
            entry = getSnapshot();
        } catch (_) {
            continue;
        }
        if (!entry) continue;
        result.total += 1;
        const playing = !!entry.playing;
        const hidden = !!entry.hidden || suppressionOwners.size > 0;
        if (playing) result.playing += 1;
        if (playing && hidden) result.playingHidden += 1;
        if (entry.suspended) result.suspended += 1;
        if (entry.hibernated) result.hibernated += 1;
        incrementCount(result.roles, entry.role);
        for (const reason of entry.suspensionReasons || []) {
            incrementCount(result.suspensionReasons, reason);
        }
    }
    return result;
}

// Test-only reset: exported so isolated Node tests do not leak module singleton
// state between assertions. Production callers never need it.
export function _resetGraphPreviewOwnershipForTests() {
    suppressionOwners.clear();
    suppressionListeners.clear();
    previewDiagnostics.clear();
}
